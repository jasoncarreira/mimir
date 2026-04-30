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
async def test_dm_target_pulls_public_cross_channel_context(tmp_path: Path):
    """Cross-pull into a DM target is one-directional: Alice's public #eng
    activity SHOULD surface inside her private DM with the bot (it's her
    own public messages — useful context, not a leak). Only DM *messages*
    are blocked from cross-pull, regardless of target.
    """
    buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
    now = datetime.now(tz=timezone.utc)

    # Alice in #eng — public, should surface in her DM with the bot.
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
    # Alice in her DM with the bot.
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
    contents = [m.content for m in activity]
    assert "public-eng-content" in contents, "public cross-channel context should surface in DM"
    assert "dm-context" in contents, "within-DM history should also surface"


@pytest.mark.asyncio
async def test_cross_platform_pull_resolves_canonical(tmp_path: Path):
    """FUTURE_WORK §6.1: Alice on slack and Alice on discord — when an
    IdentityResolver maps both platform ids to the canonical 'alice', a
    turn for slack-Alice in #help cross-pulls her discord-Alice activity
    in #eng (and vice versa)."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases:
                  - slack-U123ABC
                  - discord-456789
            """
        )
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        resolver=resolver,
    )
    now = datetime.now(tz=timezone.utc)

    # Alice on Slack in #eng (cross-pull source).
    await buf.append(
        buf.make_message(
            channel_id="slack-eng",
            kind="user_message",
            content="slack-eng activity",
            author="slack-U123ABC",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="slack",
        )
    )
    # Alice on Discord in #eng (also cross-pull source).
    await buf.append(
        buf.make_message(
            channel_id="discord-eng",
            kind="user_message",
            content="discord-eng activity",
            author="discord-456789",
            ts=(now - timedelta(minutes=5)).isoformat(),
            source="discord",
        )
    )

    # Composing a reply for Alice on a *third* platform — should pull from
    # both prior platforms because the resolver collapses them onto 'alice'.
    cross = buf.cross_author_messages(
        author="bsky:alice.bsky.social",  # not yet aliased; see next assertion
        exclude_channel="bsky-feed",
        limit=10,
        within_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    # Without alice's bsky alias mapped, target canonical is the raw bsky
    # handle and won't match. No cross-pull.
    assert cross == []

    # Now do the realistic case — composing for slack-U123ABC pulls
    # discord-eng activity (and slack-eng if exclude-channel allowed it).
    cross = buf.cross_author_messages(
        author="slack-U123ABC",
        exclude_channel="slack-eng",
        limit=10,
        within_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    contents = [m.content for m in cross]
    assert "discord-eng activity" in contents
    # slack-eng excluded by exclude_channel.
    assert "slack-eng activity" not in contents


@pytest.mark.asyncio
async def test_cross_platform_pull_kill_switch_disabled(tmp_path: Path):
    """``cross_platform_pull=False`` disables resolver-based matching;
    cross-pull falls back to direct equality. For an operator that wants
    strict per-platform isolation."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U123, discord-456]
            """
        )
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        resolver=resolver,
        cross_platform_pull=False,
    )
    now = datetime.now(tz=timezone.utc)
    await buf.append(
        buf.make_message(
            channel_id="discord-eng",
            kind="user_message",
            content="discord-eng activity",
            author="discord-456",
            ts=(now - timedelta(minutes=5)).isoformat(),
            source="discord",
        )
    )

    # Even though the resolver knows slack-U123 ≡ discord-456, the kill
    # switch disables canonical resolution — direct equality means the
    # discord message doesn't surface for the slack target.
    cross = buf.cross_author_messages(
        author="slack-U123",
        exclude_channel="slack-eng",
        limit=10,
        within_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    assert cross == []


@pytest.mark.asyncio
async def test_identity_block_surfaces_in_turn_prompt(tmp_path: Path):
    """When an inbound's author has an identity record, the turn prompt
    gets a 'Known identities' preamble + the recent activity uses the
    canonical's display_name. Cross-platform messages from the same
    person dedupe to one identity entry."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                display_name: Alice Smith
                aliases: [slack-U123, discord-456]
                notes: Eng team lead
              - canonical: bob
                display_name: Bob
                aliases: [slack-U777]
            """
        )
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        resolver=resolver,
    )
    now = datetime.now(tz=timezone.utc)
    # Alice on Slack
    await buf.append(
        buf.make_message(
            channel_id="slack-eng",
            kind="user_message",
            content="slack-eng activity",
            author="slack-U123",
            author_display="alice_slack",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="slack",
        )
    )
    # Alice on Discord (same person, different platform)
    await buf.append(
        buf.make_message(
            channel_id="discord-eng",
            kind="user_message",
            content="discord-eng activity",
            author="discord-456",
            author_display="alice#1234",
            ts=(now - timedelta(minutes=5)).isoformat(),
            source="discord",
        )
    )
    # Stranger on slack — no identity record
    await buf.append(
        buf.make_message(
            channel_id="slack-eng",
            kind="user_message",
            content="random",
            author="slack-UUNKNOWN",
            author_display="random_user",
            ts=(now - timedelta(minutes=2)).isoformat(),
            source="slack",
        )
    )

    recent = list(buf._all)
    inbound = AgentEvent(
        trigger="user_message",
        channel_id="slack-help",
        content="hi",
        author="slack-U123",
        author_display="alice_slack",
    )
    prompt = build_turn_prompt(
        inbound,
        recent_messages=recent,
        recent_message_chars=0,
        resolver=resolver,
    )

    # Identity block appears, mentions alice (deduped across platforms),
    # her display name, notes, and aliases.
    assert "## Known identities" in prompt
    assert "**alice**" in prompt
    assert "Alice Smith" in prompt
    assert "Eng team lead" in prompt
    assert "slack-U123" in prompt and "discord-456" in prompt
    # Bob is not in the recent window or inbound — skipped.
    assert "**bob**" not in prompt

    # Recent activity uses canonical's display_name, not per-message.
    assert "Alice Smith: slack-eng activity" in prompt
    assert "Alice Smith: discord-eng activity" in prompt
    # Stranger has no record — falls back to per-message author_display.
    assert "random_user: random" in prompt

    # Header uses display name, not the raw matching key.
    assert "author: alice_slack" in prompt or "author: Alice Smith" in prompt


@pytest.mark.asyncio
async def test_identity_block_absent_when_no_records_match(tmp_path: Path):
    """No identity records for any author → no Known identities section
    rendered (don't pollute the prompt with an empty block)."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U999]   # different alias from what's in the buffer
            """
        )
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        resolver=resolver,
    )
    now = datetime.now(tz=timezone.utc)
    await buf.append(
        buf.make_message(
            channel_id="slack-eng",
            kind="user_message",
            content="hi",
            author="slack-UDIFFERENT",   # not in identities.yaml
            author_display="someone",
            ts=now.isoformat(),
            source="slack",
        )
    )

    inbound = AgentEvent(
        trigger="user_message",
        channel_id="slack-eng",
        content="ok",
        author="slack-UDIFFERENT",
        author_display="someone",
    )
    prompt = build_turn_prompt(
        inbound,
        recent_messages=list(buf._all),
        resolver=resolver,
    )
    assert "## Known identities" not in prompt
    # Falls back to per-message display.
    assert "someone: hi" in prompt


@pytest.mark.asyncio
async def test_resolver_does_not_override_dm_rule(tmp_path: Path):
    """Identity reconciliation never lifts DM content into a non-DM
    channel. The DM filter wins regardless of canonical match."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U123, discord-456]
            """
        )
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        resolver=resolver,
    )
    now = datetime.now(tz=timezone.utc)

    # Alice's DM (would canonical-match if not for the DM rule).
    await buf.append(
        buf.make_message(
            channel_id="dm-discord-456",
            kind="user_message",
            content="alice-dm-secret",
            author="discord-456",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="discord",
        )
    )

    cross = buf.cross_author_messages(
        author="slack-U123",  # canonical-matches discord-456 via resolver
        exclude_channel="slack-eng",
        limit=10,
        within_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    contents = [m.content for m in cross]
    assert "alice-dm-secret" not in contents


@pytest.mark.asyncio
async def test_dm_messages_never_pulled_regardless_of_target(tmp_path: Path):
    """Source-side privacy: a DM message NEVER appears in cross-pull, no
    matter what the target channel is — even if the target is itself a
    different DM. The privacy rule is about the source, not the target.
    """
    buf = MessageBuffer(history_path=tmp_path / "chat_history.jsonl")
    now = datetime.now(tz=timezone.utc)

    # Alice's DM with the bot.
    await buf.append(
        buf.make_message(
            channel_id="dm-slack-alice",
            kind="user_message",
            content="alice-dm-secret",
            author="alice",
            ts=(now - timedelta(minutes=10)).isoformat(),
            source="slack",
        )
    )
    # Alice in #eng (so within-channel for the eng target isn't empty).
    await buf.append(
        buf.make_message(
            channel_id="eng",
            kind="user_message",
            content="eng-context",
            author="alice",
            ts=(now - timedelta(minutes=2)).isoformat(),
            source="slack",
        )
    )

    activity = buf.assemble_recent_activity(
        channel_id="eng",
        author="alice",
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
        source_allowlist=frozenset({"slack", "discord", "web"}),
    )
    contents = [m.content for m in activity]
    assert "alice-dm-secret" not in contents
    assert "eng-context" in contents
