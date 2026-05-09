"""Tests for the TurnLifecycleHook protocol + orchestrator helpers (CR#15).

The hooks themselves wrap concrete subsystems (RateLimitStore, SubagentInbox,
IndexGenerator, git_tracking, ChannelRegistry); end-to-end verification of
those subsystems lives in their own test files. Here we pin the *protocol*
contract: hooks fire in registration order, exceptions in one hook don't
propagate or block subsequent hooks, the four seam methods have working
defaults, and per-turn state on ctx is shared correctly across hooks at
the same seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mimir.event_logger import init_logger
from mimir.models import AgentEvent, TurnContext, TurnRecord
from mimir.turn_hooks import (
    TurnLifecycleHook,
    fire_finalize,
    fire_on_message,
    fire_post_query,
    fire_pre_query,
)


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-hooks")


def _make_ctx() -> TurnContext:
    return TurnContext(
        turn_id="t1",
        session_id="c-1",
        trigger="user_message",
        channel_id="c-1",
        started_at=0.0,
    )


def _make_event() -> AgentEvent:
    return AgentEvent(trigger="user_message", channel_id="c-1", content="hi")


def _make_record() -> TurnRecord:
    return TurnRecord(
        ts="2026-05-08T00:00:00Z",
        turn_id="t1",
        session_id="c-1",
        saga_session_id=None,
        trigger="user_message",
        channel_id="c-1",
        input="hi",
        output="ok",
    )


# ─── Protocol defaults ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_hook_methods_are_no_ops():
    """A bare TurnLifecycleHook (no overrides) must accept all four
    seam calls without raising. Pins that subclasses can override
    only the methods they need."""
    hook = TurnLifecycleHook()
    ctx = _make_ctx()
    event = _make_event()
    record = _make_record()
    # Each method should return None and not raise.
    assert await hook.pre_query(ctx, event) is None
    assert await hook.on_message(ctx, event, object()) is None
    assert await hook.post_query(
        ctx, event,
        messages=[], output="", error=None, options=object(),
    ) is None
    assert await hook.finalize(ctx, event, record) is None


# ─── Ordering ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hooks_fire_in_registration_order():
    """The orchestrator iterates the list in order; each helper
    (pre_query, on_message, post_query, finalize) preserves it."""
    order: list[str] = []

    class _Hook(TurnLifecycleHook):
        def __init__(self, tag: str) -> None:
            self.name = tag
            self._tag = tag

        async def pre_query(self, ctx, event):
            order.append(f"pre:{self._tag}")

        async def on_message(self, ctx, event, msg):
            order.append(f"msg:{self._tag}")

        async def post_query(self, ctx, event, **kw):
            order.append(f"post:{self._tag}")

        async def finalize(self, ctx, event, record):
            order.append(f"fin:{self._tag}")

    hooks = [_Hook("a"), _Hook("b"), _Hook("c")]
    ctx, event, record = _make_ctx(), _make_event(), _make_record()

    await fire_pre_query(hooks, ctx, event)
    await fire_on_message(hooks, ctx, event, object())
    await fire_post_query(
        hooks, ctx, event,
        messages=[], output="", error=None, options=object(),
    )
    await fire_finalize(hooks, ctx, event, record)

    assert order == [
        "pre:a", "pre:b", "pre:c",
        "msg:a", "msg:b", "msg:c",
        "post:a", "post:b", "post:c",
        "fin:a", "fin:b", "fin:c",
    ]


# ─── Exception isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_exception_does_not_block_subsequent_hooks():
    """A misbehaving hook can't sink the turn. The orchestrator
    catches Exception in each helper and logs; the next hook still
    fires. Pins per-seam isolation across all four helpers."""
    seen: list[str] = []

    class _Boom(TurnLifecycleHook):
        name = "boom"
        async def pre_query(self, ctx, event):
            raise RuntimeError("pre boom")
        async def on_message(self, ctx, event, msg):
            raise RuntimeError("msg boom")
        async def post_query(self, ctx, event, **kw):
            raise RuntimeError("post boom")
        async def finalize(self, ctx, event, record):
            raise RuntimeError("fin boom")

    class _Ok(TurnLifecycleHook):
        name = "ok"
        async def pre_query(self, ctx, event):
            seen.append("pre")
        async def on_message(self, ctx, event, msg):
            seen.append("msg")
        async def post_query(self, ctx, event, **kw):
            seen.append("post")
        async def finalize(self, ctx, event, record):
            seen.append("fin")

    hooks = [_Boom(), _Ok()]
    ctx, event, record = _make_ctx(), _make_event(), _make_record()

    await fire_pre_query(hooks, ctx, event)
    await fire_on_message(hooks, ctx, event, object())
    await fire_post_query(
        hooks, ctx, event,
        messages=[], output="", error=None, options=object(),
    )
    await fire_finalize(hooks, ctx, event, record)

    assert seen == ["pre", "msg", "post", "fin"], (
        "Boom hook's exceptions must not have prevented Ok hook from firing"
    )


# ─── Shared per-turn state via ctx ───────────────────────────────────


@pytest.mark.asyncio
async def test_hooks_share_per_turn_state_via_ctx():
    """Hooks store per-turn state on the ctx (NOT on self) so
    multi-channel concurrent turns don't corrupt each other. Pins
    the contract via ctx.task_descriptions used by the real
    SubagentLifecycleHook."""
    ctx = _make_ctx()
    event = _make_event()

    class _Writer(TurnLifecycleHook):
        async def on_message(self, ctx, event, msg):
            ctx.task_descriptions["task-A"] = "wrote it"

    class _Reader(TurnLifecycleHook):
        async def on_message(self, ctx, event, msg):
            self.captured = ctx.task_descriptions.get("task-A")

    writer = _Writer()
    reader = _Reader()
    reader.captured = None

    await fire_on_message([writer, reader], ctx, event, object())

    assert reader.captured == "wrote it"


# ─── Built-in hooks: smoke test the simple ones ──────────────────────


@pytest.mark.asyncio
async def test_index_rebuild_hook_marks_dirty_and_flushes():
    """IndexRebuildHook is one of the simplest finalize hooks —
    pin its interaction with IndexGenerator."""
    from mimir.turn_hooks import IndexRebuildHook

    indexes = AsyncMock()
    indexes.mark_dirty = lambda what: setattr(indexes, "_marked", what)
    indexes.flush = AsyncMock()
    hook = IndexRebuildHook(indexes=indexes)

    await hook.finalize(_make_ctx(), _make_event(), _make_record())

    assert indexes._marked == "all"
    indexes.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_typing_hook_handles_missing_channel_registry():
    """When the agent has no channels (test/bench paths), the hook
    must no-op silently."""
    from mimir.turn_hooks import CancelTypingHook
    hook = CancelTypingHook(channels=None)
    await hook.finalize(_make_ctx(), _make_event(), _make_record())
    # No exception, no nothing — just returns.


@pytest.mark.asyncio
async def test_cancel_typing_hook_swallows_bridge_exceptions():
    """Typing is best-effort. A bridge exception must not propagate
    (would mask the actual TurnRecord)."""
    from mimir.turn_hooks import CancelTypingHook

    class _BadBridge:
        async def cancel_typing(self, channel_id):
            raise RuntimeError("typing failed")

    class _Channels:
        def find(self, channel_id):
            return _BadBridge()

    hook = CancelTypingHook(channels=_Channels())
    # No raise.
    await hook.finalize(_make_ctx(), _make_event(), _make_record())


@pytest.mark.asyncio
async def test_post_message_saga_hook_skips_on_error():
    """The post-message saga hook (mark_contributions + synthesis-flag
    audit) only fires for successful turns. Pins the gate."""
    from mimir.turn_hooks import PostMessageSagaHook

    called = []

    async def _fake_hook(ctx, output):
        called.append(output)

    hook = PostMessageSagaHook(hook_fn=_fake_hook)
    ctx, event = _make_ctx(), _make_event()

    # Error path → skip.
    await hook.post_query(
        ctx, event,
        messages=[], output="reply", error="boom", options=object(),
    )
    assert called == []

    # Success path → fire.
    await hook.post_query(
        ctx, event,
        messages=[], output="reply", error=None, options=object(),
    )
    assert called == ["reply"]


@pytest.mark.asyncio
async def test_plan_quota_capture_hook_skips_on_error():
    """Same gate shape as post_message_saga — error-path skip."""
    from mimir.turn_hooks import PlanQuotaCaptureHook

    called: list = []

    async def _fake_capture(options):
        called.append(options)

    hook = PlanQuotaCaptureHook(capture_fn=_fake_capture)
    ctx, event = _make_ctx(), _make_event()
    sentinel_options = object()

    await hook.post_query(
        ctx, event,
        messages=[], output="", error="boom", options=sentinel_options,
    )
    assert called == []

    await hook.post_query(
        ctx, event,
        messages=[], output="", error=None, options=sentinel_options,
    )
    assert called == [sentinel_options]


@pytest.mark.asyncio
async def test_plan_quota_capture_hook_swallows_capture_exception():
    """Capture failure is logged but doesn't propagate — turn-level
    hook isolation guarantees the orchestrator can move on."""
    from mimir.turn_hooks import PlanQuotaCaptureHook

    async def _bad_capture(options):
        raise RuntimeError("capture boom")

    hook = PlanQuotaCaptureHook(capture_fn=_bad_capture)
    # No raise.
    await hook.post_query(
        _make_ctx(), _make_event(),
        messages=[], output="", error=None, options=object(),
    )


# ─── WikiBacklinksHook ───────────────────────────────────────────────


def _wiki_ctx() -> TurnContext:
    """TurnContext with monotonic ``started_at`` — matches what
    ``Agent.run_turn`` actually constructs in production. The hook
    must NOT compare st_mtime against this value (different clock
    domain); it uses a snapshot dict on ctx instead."""
    import time as _time
    return TurnContext(
        turn_id="t1",
        session_id="c-1",
        trigger="user_message",
        channel_id="c-1",
        started_at=_time.monotonic(),
    )


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_regenerates_on_wiki_edit(tmp_path: Path):
    """A wiki page modified between pre_query and finalize triggers a
    regen. Snapshot-based detection works regardless of how long the
    process has been running (the prior implementation compared
    ``ctx.started_at`` (monotonic) against ``st_mtime`` (wall-clock),
    which was always-true on any production-aged process)."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    page = wiki / "concepts" / "foo.md"
    page.write_text("# Foo, no links\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    await hook.pre_query(ctx, _make_event())

    # Bot edits the page during the turn.
    import time as _time
    _time.sleep(0.01)  # ensure mtime resolution distinguishes the write
    page.write_text("# Foo with [[ghost]] dangling link\n", encoding="utf-8")

    await hook.finalize(ctx, _make_event(), _make_record())

    assert (wiki / "orphans.md").exists()
    assert (wiki / "dangling-links.md").exists()
    assert (wiki / "backlinks-index.md").exists()
    assert "foo" in (wiki / "orphans.md").read_text()
    assert "ghost" in (wiki / "dangling-links.md").read_text()


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_skips_when_no_wiki_pages_changed(tmp_path: Path):
    """When the snapshot at finalize matches the snapshot at pre_query
    (no wiki pages touched), the hook MUST not re-run. This is the
    test that fails under the old monotonic-vs-wall-clock comparison
    bug — it'd regen on every turn even when nothing changed."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    page = wiki / "concepts" / "foo.md"
    page.write_text("# Stable content\n", encoding="utf-8")

    # Pre-existing generated outputs from a prior turn.
    for name in ("orphans.md", "dangling-links.md", "backlinks-index.md"):
        (wiki / name).write_text(f"stale-{name}\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    await hook.pre_query(ctx, _make_event())
    # No edits between pre_query and finalize.
    await hook.finalize(ctx, _make_event(), _make_record())

    # Outputs were NOT overwritten — stale content survives.
    assert (wiki / "orphans.md").read_text() == "stale-orphans.md\n"
    assert (wiki / "dangling-links.md").read_text() == "stale-dangling-links.md\n"
    assert (wiki / "backlinks-index.md").read_text() == "stale-backlinks-index.md\n"


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_skips_when_only_generated_outputs_changed(tmp_path: Path):
    """If the only files newer than the snapshot are the 3 generated
    outputs themselves, the hook must NOT re-fire — otherwise the
    very act of regenerating triggers another regen on the next turn
    forever."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    page = wiki / "concepts" / "foo.md"
    page.write_text("# Stable\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    await hook.pre_query(ctx, _make_event())

    # Simulate a prior turn's wiki_backlinks output writes — these
    # occur AFTER pre_query but they're in _WIKI_GENERATED_OUTPUTS,
    # so the hook should ignore them.
    import time as _time
    _time.sleep(0.01)
    for name in ("orphans.md", "dangling-links.md", "backlinks-index.md"):
        (wiki / name).write_text("from-prior-turn\n", encoding="utf-8")

    await hook.finalize(ctx, _make_event(), _make_record())

    # The 3 outputs were NOT overwritten by this turn's regen — the
    # snapshot ignored them, no content page changed, so no regen.
    assert (wiki / "orphans.md").read_text() == "from-prior-turn\n"
    assert (wiki / "dangling-links.md").read_text() == "from-prior-turn\n"
    assert (wiki / "backlinks-index.md").read_text() == "from-prior-turn\n"


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_regenerates_on_new_page(tmp_path: Path):
    """A page added between pre_query and finalize (not in the
    snapshot at all) must trigger regen. Pins the new-page branch of
    the touched-detection logic."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "existing.md").write_text("# Stable\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    await hook.pre_query(ctx, _make_event())

    # Bot creates a brand-new page during the turn.
    (wiki / "concepts" / "fresh.md").write_text("# New\n", encoding="utf-8")

    await hook.finalize(ctx, _make_event(), _make_record())
    assert (wiki / "orphans.md").exists()
    # Both pages are orphans (no inbound).
    assert "fresh" in (wiki / "orphans.md").read_text()
    assert "existing" in (wiki / "orphans.md").read_text()


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_regenerates_on_page_removal(tmp_path: Path):
    """A page in the snapshot but missing at finalize triggers regen.
    Without this branch, deleting a page wouldn't update orphans.md
    until the NEXT turn that edits something."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    page = wiki / "concepts" / "doomed.md"
    page.write_text("# Will be deleted\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    await hook.pre_query(ctx, _make_event())

    page.unlink()

    await hook.finalize(ctx, _make_event(), _make_record())
    assert (wiki / "orphans.md").exists()
    # Deleted page is gone from the report.
    assert "doomed" not in (wiki / "orphans.md").read_text()


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_no_op_when_no_wiki_dir(tmp_path: Path):
    """A home with no state/wiki/ at all → no error, no work."""
    from mimir.turn_hooks import WikiBacklinksHook

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    # No raise from either pre_query or finalize.
    await hook.pre_query(ctx, _make_event())
    await hook.finalize(ctx, _make_event(), _make_record())


@pytest.mark.asyncio
async def test_wiki_backlinks_hook_handles_missing_pre_query_snapshot(tmp_path: Path):
    """Tests / direct calls that don't run pre_query before finalize
    must not crash. Since the snapshot defaults to ``{}``, every page
    in the wiki appears 'new' → triggers a regen. That's the safe
    default: better to regen unnecessarily once than to skip a real
    health regression."""
    from mimir.turn_hooks import WikiBacklinksHook

    wiki = tmp_path / "state" / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "page.md").write_text("# Page\n", encoding="utf-8")

    ctx = _wiki_ctx()
    hook = WikiBacklinksHook(home=tmp_path)
    # Skip pre_query — finalize sees empty snapshot.
    await hook.finalize(ctx, _make_event(), _make_record())
    # Regen ran (because empty snapshot != current state).
    assert (wiki / "orphans.md").exists()
