"""Phase 6.8 — concurrency hardening (SPEC §4.5).

Covers the SPEC scenarios that aren't already in ``test_dispatcher.py`` and
``test_history.py``:

- Global semaphore stress: 20 channels at ``MIMIR_MAX_CONCURRENT_TURNS=5``
  drains all channels in per-channel FIFO with peak in-flight ≤ 5.
- DM privacy in the assembled prompt path: DM messages do not surface in
  cross-channel author pull when the bot is replying in a non-DM channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.config import Config
from mimir.dispatcher import Dispatcher
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.models import AgentEvent


def _make_config(home: Path, **overrides) -> Config:
    cfg = Config.from_env()
    return replace(
        cfg,
        home=home,
        max_concurrent_turns=overrides.get("max_concurrent_turns", 5),
        max_channel_queue=overrides.get("max_channel_queue", 100),
        worker_idle_timeout_s=overrides.get("worker_idle_timeout_s", 1),
    )


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


# ---- Global semaphore stress (SPEC §4.5) -------------------------------


@pytest.mark.asyncio
async def test_twenty_channels_at_max_five_drain_in_per_channel_order(tmp_path: Path):
    """20 channels × 5 events each at ``max_concurrent_turns=5``.

    Per-channel FIFO must be preserved even though the global cap forces
    cross-channel interleaving. Peak in-flight must never exceed the cap.
    """
    cfg = _make_config(tmp_path, max_concurrent_turns=5, max_channel_queue=20)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()
    seen: dict[str, list[int]] = {}

    async def runner(event: AgentEvent) -> None:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Stagger work so contention is real, not theoretical.
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
            seen.setdefault(event.channel_id, []).append(int(event.content))

    disp = Dispatcher(cfg, runner)
    for ch in range(20):
        for seq in range(5):
            ok = await disp.enqueue(
                AgentEvent(trigger="x", channel_id=f"c{ch}", content=str(seq))
            )
            assert ok, f"queue rejected c{ch}/{seq} unexpectedly"

    await disp.drain()

    # Per-channel order strictly preserved.
    for ch in range(20):
        assert seen[f"c{ch}"] == [0, 1, 2, 3, 4], (
            f"c{ch} reordered: {seen[f'c{ch}']}"
        )
    assert peak <= 5, f"global semaphore breached: peak={peak}"
    assert peak >= 2, f"test didn't actually stress concurrency: peak={peak}"


@pytest.mark.asyncio
async def test_queue_full_emits_admission_rejected(tmp_path: Path):
    """An event rejected by ``max_channel_queue=1`` lands as
    ``event_admission_rejected`` in events.jsonl with the channel_id."""
    cfg = _make_config(tmp_path, max_channel_queue=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(event: AgentEvent) -> None:
        started.set()
        await release.wait()

    disp = Dispatcher(cfg, runner)
    assert await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="a"))
    await started.wait()
    assert await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="b"))
    rejected = await disp.enqueue(AgentEvent(trigger="x", channel_id="c1", content="c"))
    assert rejected is False
    release.set()
    await disp.drain()

    events_log = tmp_path / "logs" / "events.jsonl"
    text = events_log.read_text()
    assert '"event_admission_rejected"' in text
    assert '"channel_id": "c1"' in text


# ---- DM privacy in assembled prompt (SPEC §5.4) -------------------------


@pytest.mark.asyncio
async def test_dm_messages_excluded_from_cross_channel_pull_in_eng(tmp_path: Path):
    """Alice in #eng + a DM from Alice to the bot. When the bot composes a reply
    in #eng, only Alice's #eng messages must surface in cross-pull (none of
    Alice's DM content)."""
    buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
    now = datetime.now(tz=timezone.utc)

    # Alice in #help (cross-channel, public, fair game)
    await buf.append(
        buf.make_message(
            channel_id="help",
            kind="user_message",
            content="public stuff in #help",
            author="alice",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="slack",
        )
    )
    # Alice DMs the bot (private; SPEC §5.4 says NEVER cross-pulled)
    await buf.append(
        buf.make_message(
            channel_id="dm-slack-alice",
            kind="user_message",
            content="my-dm-secret-12345",
            author="alice",
            ts=(now - timedelta(minutes=5)).isoformat(),
            source="slack",
        )
    )

    # Bot is replying in #eng. DM content must NOT appear in the assembled prompt.
    activity = buf.assemble_recent_activity(
        channel_id="eng",
        author="alice",
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    contents = [m.content for m in activity]
    assert "my-dm-secret-12345" not in contents
    assert "public stuff in #help" in contents


@pytest.mark.asyncio
async def test_dm_target_skips_cross_channel_pull(tmp_path: Path):
    """SPEC §5.4: cross-channel pull is skipped when the current channel is a
    DM. Seed the DM with its own messages so the within-channel deque is
    populated (avoiding the empty-deque global fallback path, which is a
    separate code path with separate semantics).
    """
    buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
    now = datetime.now(tz=timezone.utc)

    # Alice in #eng (would be cross-pulled if the target were public)
    await buf.append(
        buf.make_message(
            channel_id="eng",
            kind="user_message",
            content="public-eng-content",
            author="alice",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="slack",
        )
    )
    # Alice in her DM with the bot (within-channel context for the DM target)
    await buf.append(
        buf.make_message(
            channel_id="dm-slack-alice",
            kind="user_message",
            content="dm-context",
            author="alice",
            ts=(now - timedelta(minutes=2)).isoformat(),
            source="slack",
        )
    )

    activity = buf.assemble_recent_activity(
        channel_id="dm-slack-alice",
        author="alice",
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    # Cross-pull is skipped — only the DM's own messages surface.
    assert {m.channel_id for m in activity} == {"dm-slack-alice"}
    assert "public-eng-content" not in [m.content for m in activity]
