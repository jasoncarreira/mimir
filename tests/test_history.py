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
    # Per-channel cap of 2 still applies to the per-channel deque…
    assert buf.total_count() == 3
    assert buf.channel_count("c1") == 2
    # The exact-channel read honors the per-channel cap.
    contents = [m.content for m in buf.recent_for_channel("c1", 10)]
    assert contents == ["3", "4"]


@pytest.mark.asyncio
async def test_recent_for_channel_is_exactly_channel_scoped(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    await buf.append(buf.make_message(
        channel_id="eng", kind="user_message", content="local", author="alice",
    ))
    await buf.append(buf.make_message(
        channel_id="ops", kind="user_message", content="unrelated", author="bob",
    ))
    assert [m.content for m in buf.recent_for_channel("eng", 10)] == ["local"]
    assert buf.recent_for_channel("empty", 10) == []


@pytest.mark.asyncio
async def test_assemble_public_activity_pulls_same_user_not_global_pool(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    for channel, kind, content, author, offset in [
        ("eng", "user_message", "alice-elsewhere", "alice", -3),
        ("eng", "assistant_message", "assistant-reply", None, -2),
        ("ops", "user_message", "bob-unrelated", "bob", -1),
        ("help", "user_message", "alice-current", "alice", 0),
    ]:
        await buf.append(buf.make_message(
            channel_id=channel, kind=kind, content=content, author=author,
            ts=_now_iso(offset_minutes=offset),
        ))
    out = buf.assemble_recent_activity(
        channel_id="help", author="alice", recent_per_channel=10,
        recent_author_cross=10, cross_hours=24,
    )
    assert [m.content for m in out] == [
        "alice-elsewhere", "assistant-reply", "alice-current",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_channel", ["help", "dm-slack-alice"])
async def test_assemble_activity_excludes_other_users_adjacent_replies(
    tmp_path: Path, target_channel: str,
):
    """Only assistant replies following the target user's anchor may cross channels."""
    buf = _make_buffer(tmp_path)
    for channel, kind, content, author, offset in [
        ("general", "user_message", "alice-anchor", "alice", -4),
        ("general", "assistant_message", "reply-to-alice", None, -3),
        ("general", "user_message", "bob-secret", "bob", -2),
        ("general", "assistant_message", "reply-to-bob", None, -1),
        (target_channel, "user_message", "alice-current", "alice", 0),
    ]:
        await buf.append(buf.make_message(
            channel_id=channel, kind=kind, content=content, author=author,
            ts=_now_iso(offset_minutes=offset),
        ))

    out = buf.assemble_recent_activity(
        channel_id=target_channel, author="alice", recent_per_channel=10,
        recent_author_cross=10, cross_hours=24,
    )

    assert [m.content for m in out] == [
        "alice-anchor", "reply-to-alice", "alice-current",
    ]


@pytest.mark.asyncio
async def test_assemble_public_activity_resolves_cross_platform_aliases(tmp_path: Path):
    class Resolver:
        def resolve(self, author):
            return {"discord-alice": "alice", "slack-alice": "alice"}.get(author, author)

    buf = MessageBuffer(history_path=tmp_path / "chat.jsonl", resolver=Resolver())
    await buf.append(buf.make_message(
        channel_id="discord-eng", kind="user_message", content="same-person",
        author="discord-alice", ts=_now_iso(offset_minutes=-1),
    ))
    out = buf.assemble_recent_activity(
        channel_id="slack-help", author="slack-alice", recent_per_channel=10,
        recent_author_cross=10, cross_hours=24,
    )
    assert [m.content for m in out] == ["same-person"]


@pytest.mark.asyncio
async def test_assemble_dm_activity_never_imports_other_users_dms(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    for channel, content, author, offset in [
        ("dm-discord-alice", "alice-private", "alice", -2),
        ("dm-discord-bob", "bob-secret", "bob", -1),
        ("dm-slack-alice", "alice-current", "alice", 0),
    ]:
        await buf.append(buf.make_message(
            channel_id=channel, kind="user_message", content=content, author=author,
            ts=_now_iso(offset_minutes=offset),
        ))
    out = buf.assemble_recent_activity(
        channel_id="dm-slack-alice", author="alice", recent_per_channel=10,
        recent_author_cross=10, cross_hours=24,
    )
    assert [m.content for m in out] == ["alice-private", "alice-current"]


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
async def test_cross_author_excludes_dms_by_default(tmp_path: Path):
    buf = _make_buffer(tmp_path)
    await buf.append(buf.make_message(
        channel_id="dm-discord-alice", kind="user_message",
        content="private", author="alice",
    ))
    out = buf.cross_author_messages(
        author="alice", exclude_channel="dm-slack-alice", limit=5, within_hours=24,
    )
    assert out == []


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


@pytest.mark.asyncio
async def test_concurrent_appends_run_in_parallel_on_thread_pool(tmp_path: Path):
    """CR#17 regression: the previous ``async with self._write_lock``
    held the lock across ``await asyncio.to_thread(self._append_disk)``,
    which serialized every concurrent append through a single thread
    and defeated the to_thread parallelism. Without the lock, two
    concurrent appends must reach ``_append_disk`` in separate threads
    at the same time.

    This pins the throughput fix and asserts both lines still land on
    disk safely (POSIX O_APPEND atomicity)."""
    import threading

    buf = _make_buffer(tmp_path)

    inside = threading.Event()
    seen_two = threading.Event()
    release = threading.Event()
    in_flight_count = 0
    in_flight_lock = threading.Lock()
    seen_thread_ids: set[int] = set()

    original_append_disk = buf._append_disk

    def _gated_append_disk(msg: Message) -> None:
        nonlocal in_flight_count
        with in_flight_lock:
            in_flight_count += 1
            seen_thread_ids.add(threading.get_ident())
            if in_flight_count >= 2:
                seen_two.set()
        inside.set()
        # Block until the test releases — both threads must be parked
        # here at once for the assertion to fire.
        release.wait(timeout=2.0)
        with in_flight_lock:
            in_flight_count -= 1
        original_append_disk(msg)

    buf._append_disk = _gated_append_disk

    msg_a = buf.make_message(
        channel_id="c1", kind="user_message", content="a", author="alice"
    )
    msg_b = buf.make_message(
        channel_id="c1", kind="user_message", content="b", author="bob"
    )

    t1 = asyncio.create_task(buf.append(msg_a))
    t2 = asyncio.create_task(buf.append(msg_b))

    # Wait for both threads to be inside _append_disk simultaneously.
    # If the lock were still held across to_thread, the second append
    # would never enter — only one thread would ever be in flight.
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: seen_two.wait(timeout=2.0)
    )
    assert seen_two.is_set(), (
        "expected two concurrent _append_disk calls; the lock is back"
    )
    assert len(seen_thread_ids) == 2, (
        f"expected two distinct threads; saw {seen_thread_ids}"
    )

    release.set()
    await asyncio.gather(t1, t2)

    # Both records on disk, one per line, no interleaving.
    lines = (tmp_path / "messages" / "chat_history.jsonl").read_text().splitlines()
    assert len(lines) == 2
    contents = sorted(json.loads(line)["content"] for line in lines)
    assert contents == ["a", "b"]


def test_recent_for_channel_limit_zero_returns_empty(tmp_path: Path):
    """The bare ``[-0:]`` slice would return the full list — guard against that."""
    buf = _make_buffer(tmp_path)
    # Sync path: append into deques directly via private method for test brevity.
    buf._append_in_memory(
        buf.make_message(channel_id="c1", kind="user_message", content="a", source="slack")
    )
    assert buf.recent_for_channel("c1", limit=0) == []


# ---------------------------------------------------------------------------
# render_recent_activity channel-side resolution (chainlink #40 / #43).
# ---------------------------------------------------------------------------


def test_render_recent_activity_uses_channel_display_name(tmp_path: Path):
    """Phase C: when a resolver knows the channel id, the line renders
    ``<display_name> (<channel_id>)`` so the agent reads a friendly
    label without losing the canonical id (still needed for routing)."""
    from textwrap import dedent
    from mimir.identities import IdentityResolver

    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            channels:
              - canonical: discord-1500
                display_name: jason-mimir
                kind: public
            """
        ),
        encoding="utf-8",
    )
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()

    buf = _make_buffer(tmp_path)
    msgs = [
        buf.make_message(
            channel_id="discord-1500", kind="user_message",
            content="hi", author="alice",
        ),
        buf.make_message(
            channel_id="discord-9999",  # unknown to resolver
            kind="user_message", content="hey", author="bob",
        ),
    ]
    rendered = render_recent_activity(msgs, resolver=resolver)
    # Known channel renders with display name + id in parens.
    assert "jason-mimir (discord-1500)" in rendered
    # Unknown channel falls through to bare id.
    assert "[" in rendered and "discord-9999" in rendered
    assert "jason-mimir (discord-9999)" not in rendered


def test_render_recent_activity_no_resolver_uses_bare_channel_id(tmp_path: Path):
    """Without a resolver, channel renders bare (existing format)."""
    buf = _make_buffer(tmp_path)
    msgs = [
        buf.make_message(
            channel_id="discord-1500", kind="user_message",
            content="hi", author="alice",
        ),
    ]
    rendered = render_recent_activity(msgs)
    # Bare channel id, no display-name prefix.
    assert "discord-1500" in rendered
    assert "(" not in rendered.split("] ")[0]


def test_render_recent_activity_legacy_resolver_without_channel_api(tmp_path: Path):
    """A legacy resolver lacking ``channel_display_name`` still works
    on the author side — the channel side falls through gracefully."""

    class LegacyPeopleOnlyResolver:
        def display_name(self, author: str | None) -> str | None:
            if author == "alice":
                return "Alice S."
            return None

    buf = _make_buffer(tmp_path)
    msgs = [
        buf.make_message(
            channel_id="discord-1500", kind="user_message",
            content="hi", author="alice",
        ),
    ]
    rendered = render_recent_activity(msgs, resolver=LegacyPeopleOnlyResolver())
    # Author display rendered; channel falls through to bare id.
    assert "Alice S." in rendered
    assert "discord-1500" in rendered


@pytest.mark.asyncio
async def test_assemble_recent_activity_skips_synthetic_scheduler_channels(tmp_path: Path):
    """Chainlink #78: synthetic ``scheduler:*`` channel IDs (heartbeat,
    reflect, saga-consolidate, introspection-report) hold only prior
    assistant scheduled-tick replies — no narrative continuity. The
    within-channel pull is skipped entirely for these channels, and
    cross-channel pull is naturally already skipped when author is None
    (the normal case for scheduled ticks)."""
    buf = _make_buffer(tmp_path)
    # Populate a "prior heartbeat reply" on the synthetic channel — the
    # exact noise the fix exists to drop.
    await buf.append(
        buf.make_message(
            channel_id="scheduler:heartbeat",
            kind="assistant_message",
            content="prior heartbeat reply that should NOT leak into next tick",
            author=None,
        )
    )

    # Pin that the message IS in the buffer — the filter, not absence,
    # is the load-bearing thing (per PR #127 review tightening).
    assert len(buf.recent_for_channel("scheduler:heartbeat", limit=10)) == 1

    out = buf.assemble_recent_activity(
        channel_id="scheduler:heartbeat",
        author=None,  # ticks have no inbound author
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
    )
    assert out == []  # within-channel skipped + cross-channel skipped (no author)


@pytest.mark.asyncio
async def test_assemble_recent_activity_skips_all_scheduler_prefixed_channels(
    tmp_path: Path,
):
    """Same fix covers reflect / saga-consolidate / introspection-report
    — anything starting with ``scheduler:``."""
    buf = _make_buffer(tmp_path)
    for ch in ("scheduler:reflect", "scheduler:saga-consolidate", "scheduler:introspection-report"):
        await buf.append(
            buf.make_message(
                channel_id=ch,
                kind="assistant_message",
                content=f"prior {ch} reply",
                author=None,
            )
        )
        # Filter-vs-absence pin: the message IS in the buffer. The
        # public ``recent_for_channel`` pools across non-private
        # channels (chainlink #40), so we filter the result by
        # ``channel_id`` to isolate this iteration's contribution.
        pool = buf.recent_for_channel(ch, limit=100)
        assert any(m.channel_id == ch for m in pool)
        out = buf.assemble_recent_activity(
            channel_id=ch,
            author=None,
            recent_per_channel=10,
            recent_author_cross=10,
            cross_hours=24,
        )
        assert out == [], f"expected empty for {ch}, got {out}"


@pytest.mark.asyncio
async def test_assemble_recent_activity_skips_synthetic_poller_channels(
    tmp_path: Path,
):
    """Same fix extends to ``poller:*`` channels (PR #127 review). Each
    poller emits events on ``poller:<name>``; the agent's prior replies
    to past events on the same poller are not useful context for the
    next discrete event."""
    buf = _make_buffer(tmp_path)
    for ch in ("poller:github-activity", "poller:oauth-usage", "poller:custom-watcher"):
        await buf.append(
            buf.make_message(
                channel_id=ch,
                kind="assistant_message",
                content=f"prior {ch} reply",
                author=None,
            )
        )
        # Filter-vs-absence pin: the message IS in the buffer. The
        # public ``recent_for_channel`` pools across non-private
        # channels (chainlink #40), so we filter the result by
        # ``channel_id`` to isolate this iteration's contribution.
        pool = buf.recent_for_channel(ch, limit=100)
        assert any(m.channel_id == ch for m in pool)
        out = buf.assemble_recent_activity(
            channel_id=ch,
            author=None,
            recent_per_channel=10,
            recent_author_cross=10,
            cross_hours=24,
        )
        assert out == [], f"expected empty for {ch}, got {out}"


@pytest.mark.asyncio
async def test_assemble_recent_activity_real_channel_still_pulls(tmp_path: Path):
    """Regression guard: the chainlink #78 gate must not affect real
    channels — user_message turns must still see their channel tail."""
    buf = _make_buffer(tmp_path)
    await buf.append(
        buf.make_message(
            channel_id="discord-100000000000000002",
            kind="user_message",
            content="hello",
            author="jason",
        )
    )

    out = buf.assemble_recent_activity(
        channel_id="discord-100000000000000002",
        author="jason",
        recent_per_channel=10,
        recent_author_cross=10,
        cross_hours=24,
    )
    assert len(out) == 1
    assert out[0].content == "hello"


# ── Global-buffer accessor (used by send_message + streaming) ───────


def test_get_global_buffer_returns_none_before_set():
    """Default: no buffer registered → ``get_global_buffer`` returns
    None. The send_message tool + streaming dispatcher both have to
    handle the unregistered case (test paths that don't go through
    ``server.serve``)."""
    from mimir.history import get_global_buffer, set_global_buffer

    set_global_buffer(None)  # type: ignore[arg-type]
    assert get_global_buffer() is None


def test_set_global_buffer_makes_it_resolvable(tmp_path: Path):
    """After ``set_global_buffer`` runs, ``get_global_buffer`` returns
    the same instance. Server registers this once at startup."""
    from mimir.history import get_global_buffer, set_global_buffer

    buf = _make_buffer(tmp_path)
    set_global_buffer(buf)
    try:
        assert get_global_buffer() is buf
    finally:
        set_global_buffer(None)  # type: ignore[arg-type]


# ── Source attribution for outbound messages (chainlink #270) ────────


@pytest.mark.asyncio
async def test_outbound_message_with_correct_source_survives_allowlist(tmp_path: Path):
    """Mimir's own replies must pass the production source_allowlist filter
    so they appear in ## Recent activity on the next turn (chainlink #270).

    The fix: send_message uses bridge.name and the TurnContext.channel_source
    is set from event.source, so outbound messages get source='discord'
    (etc.) instead of None. This test pins the allowlist behaviour from
    the buffer side — the equivalent send_message-side test is
    test_send_message_records_bridge_name_as_source in test_bridge_directives.
    """
    from mimir.history import get_global_buffer, set_global_buffer

    buf = _make_buffer(tmp_path)
    prod_allowlist = frozenset({"discord", "slack", "bluesky", "web", "stdin"})

    # Simulate what send_message (post-fix) writes: source=bridge.name.
    await buf.append(
        buf.make_message(
            channel_id="discord-1500672382166110321",
            kind="assistant_message",
            content="PR #457 is ready for force-push.",
            source="discord",  # bridge.name — the post-fix behaviour
        )
    )
    # Also add a user message to confirm interleaving.
    await buf.append(
        buf.make_message(
            channel_id="discord-1500672382166110321",
            kind="user_message",
            content="Yes force push",
            author="jason",
            source="discord",
        )
    )

    # Without allowlist: both visible.
    msgs = buf.recent_for_channel("discord-1500672382166110321", limit=10)
    assert len(msgs) == 2

    # With production allowlist: both still visible (source='discord' passes).
    msgs_filtered = buf.recent_for_channel(
        "discord-1500672382166110321", limit=10, source_allowlist=prod_allowlist
    )
    assert len(msgs_filtered) == 2, (
        "Outbound message was filtered — source='discord' should pass the allowlist. "
        "Pre-fix, source=None caused all assistant messages to be excluded."
    )
    contents = {m.content for m in msgs_filtered}
    assert "PR #457 is ready for force-push." in contents
    assert "Yes force push" in contents


@pytest.mark.asyncio
async def test_outbound_source_none_excluded_by_allowlist(tmp_path: Path):
    """Pre-fix regression: source=None is excluded by the production
    allowlist. This test documents the old (broken) behaviour so the
    contrast is clear — any code that emits source=None will lose messages
    from ## Recent activity."""
    buf = _make_buffer(tmp_path)
    prod_allowlist = frozenset({"discord", "slack", "bluesky", "web", "stdin"})

    await buf.append(
        buf.make_message(
            channel_id="discord-1500672382166110321",
            kind="assistant_message",
            content="message with no source",
            source=None,  # the old broken behaviour
        )
    )

    msgs_filtered = buf.recent_for_channel(
        "discord-1500672382166110321", limit=10, source_allowlist=prod_allowlist
    )
    assert len(msgs_filtered) == 0, (
        "source=None should be excluded by the allowlist. "
        "If this fails the filter logic changed."
    )


@pytest.mark.asyncio
async def test_recent_in_channel_is_channel_scoped_and_ordered(tmp_path: Path):
    """recent_in_channel returns only that channel, oldest to newest."""
    buf = _make_buffer(tmp_path)
    await buf.append(buf.make_message(channel_id="web-a", kind="user_message", content="a1", author="x"))
    await buf.append(buf.make_message(channel_id="web-b", kind="user_message", content="b1", author="y"))
    await buf.append(buf.make_message(channel_id="web-a", kind="assistant_message", content="a2", author="mimir"))
    await buf.append(buf.make_message(channel_id="web-a", kind="user_message", content="a3", author="x"))

    assert [m.content for m in buf.recent_in_channel("web-a", 2)] == ["a2", "a3"]
    assert [m.content for m in buf.recent_in_channel("web-a", 50)] == ["a1", "a2", "a3"]
    assert buf.recent_in_channel("web-a", 0) == []
    assert buf.recent_in_channel("web-missing", 5) == []
