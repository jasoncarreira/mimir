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


# ─── PR #110 review-followup: SubagentLifecycleHook cross-turn ─────────


@pytest.mark.asyncio
async def test_subagent_lifecycle_hook_records_task_description_across_turns():
    """PR #110 review-followup: ``SubagentLifecycleHook`` stores
    task_descriptions on a process-level OrderedDict (LRU, capped at
    4096) so background-spawned subagents that complete in a LATER
    turn (the common case per ``subagent_defs.py``: ``background=True``)
    surface the description in the inbox push instead of None.

    This test fires TaskStartedMessage in turn 1, then
    TaskNotificationMessage in turn 2 (different ctx, different
    task_descriptions dict). Pre-fix the lookup returned None.
    Post-fix the hook's own registry carries the description."""
    from types import SimpleNamespace
    from claude_agent_sdk import (
        TaskNotificationMessage, TaskStartedMessage,
    )
    from mimir.subagent_inbox import SubagentInbox
    from mimir.turn_hooks import SubagentLifecycleHook

    inbox = SubagentInbox()
    hook = SubagentLifecycleHook(inbox)

    # Turn 1: subagent starts with description.
    ctx1 = _make_ctx()
    event1 = _make_event()
    started = TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id="task-xyz",
        description="implement the feature",
        uuid="u-task-xyz",
        session_id="s",
    )
    await hook.on_message(ctx1, event1, started)
    # Per-turn ctx dict has it.
    assert ctx1.task_descriptions.get("task-xyz") == "implement the feature"

    # Turn 2: completion arrives; ctx is fresh (different dict).
    ctx2 = _make_ctx()
    event2 = _make_event()
    assert "task-xyz" not in ctx2.task_descriptions  # fresh ctx
    notif = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-xyz",
        status="completed",
        output_file="",
        summary="done",
        uuid="u-task-xyz-2",
        session_id="s",
    )
    await hook.on_message(ctx2, event2, notif)

    # Inbox push carries the description from the hook-level registry,
    # NOT from ctx2.task_descriptions (which is empty).
    pushed = await inbox.drain("c-1")
    assert len(pushed) == 1
    assert pushed[0].description == "implement the feature"


def test_subagent_lifecycle_hook_lru_cap_evicts_oldest():
    """Process-level dict is capped at _TASK_DESC_LRU_CAP (4096).
    Once full, oldest-by-last-access is evicted."""
    from mimir.turn_hooks import SubagentLifecycleHook
    from mimir.subagent_inbox import SubagentInbox

    hook = SubagentLifecycleHook(SubagentInbox())
    # Set the cap to a tractable value for testing.
    hook._TASK_DESC_LRU_CAP = 5
    for i in range(7):
        hook._record_task_description(f"task-{i}", f"desc-{i}")
    # Newest 5 survive; oldest 2 evicted.
    assert "task-0" not in hook._task_descriptions
    assert "task-1" not in hook._task_descriptions
    assert "task-6" in hook._task_descriptions
    assert hook._task_descriptions.get("task-2") == "desc-2"
    assert len(hook._task_descriptions) == 5


# ─── CommitmentExtractionHook (Phase 2a) ─────────────────────────────


def _make_session_end_ctx(turn_id="t-end", saga="s-1"):
    return TurnContext(
        turn_id=turn_id,
        session_id="c-1",
        trigger="saga_session_end",
        channel_id="poller:github-activity",
        started_at=0.0,
        saga_session_id=saga,
    )


def _make_session_end_record(output: str, turn_id="t-end"):
    return TurnRecord(
        ts="2026-05-11T00:00:00Z",
        turn_id=turn_id,
        session_id="c-1",
        saga_session_id="s-1",
        trigger="saga_session_end",
        channel_id="poller:github-activity",
        input="(synthesis prompt)",
        output=output,
    )


@pytest.mark.asyncio
async def test_commitment_extraction_skips_non_session_end_triggers(tmp_path):
    """The hook only fires on ``trigger=saga_session_end``. A regular
    ``user_message`` turn must NOT trigger extraction (we'd extract
    promises out of the agent's reply text, which isn't the design)."""
    from mimir.turn_hooks import CommitmentExtractionHook
    from mimir.commitments import CommitmentsStore
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    ctx = _make_ctx()
    ctx.trigger = "user_message"
    record = _make_record()
    await hook.finalize(ctx, _make_event(), record)
    # Store is untouched.
    assert store.current_state() == {}


@pytest.mark.asyncio
async def test_commitment_extraction_skips_empty_output(tmp_path):
    """No synthesis output → nothing to extract from → no LLM call."""
    from mimir.turn_hooks import CommitmentExtractionHook
    from mimir.commitments import CommitmentsStore
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("")  # empty output
    await hook.finalize(ctx, _make_event(), record)
    assert store.current_state() == {}


@pytest.mark.asyncio
async def test_commitment_extraction_persists_records(tmp_path, monkeypatch):
    """Happy path: session-end turn with output → mocked extractor
    returns 2 records → both land in the store."""
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.models import CommitmentRecord, make_commitment_id
    from mimir.turn_hooks import CommitmentExtractionHook

    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    # Patch the extractor to return canned records.
    async def fake_extract(output, *, channel_id, saga_session_id, source_turn_id):
        return [
            CommitmentRecord(
                id=make_commitment_id(), channel_id=channel_id,
                text="Apply Item 7 fix for PR #111",
                kind="agent_promise", confidence=0.9,
                source_turn_id=source_turn_id,
                saga_session_id=saga_session_id,
            ),
            CommitmentRecord(
                id=make_commitment_id(), channel_id=None,
                text="Read paper",
                kind="open_loop", confidence=0.65,
                source_turn_id=source_turn_id,
                saga_session_id=saga_session_id,
            ),
        ]

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", fake_extract,
    )

    ctx = _make_session_end_ctx()
    # Output must clear MIN_OUTPUT_LEN (100 chars) for the hook to
    # invoke the extractor — short outputs short-circuit to the
    # ``short_output`` no-op event without calling extract_commitments.
    record = _make_session_end_record(
        "Boundary recorded. Two unfinished items carried forward: "
        "PR #111 Item 7 fix pending, and follow up on Mary's "
        "paper recommendation from last Tuesday afternoon."
    )
    await hook.finalize(ctx, _make_event(), record)

    state = store.current_state()
    assert len(state) == 2
    texts = {r.text for r in state.values()}
    assert texts == {"Apply Item 7 fix for PR #111", "Read paper"}


@pytest.mark.asyncio
async def test_commitment_extraction_dedupes_against_existing(tmp_path, monkeypatch):
    """A re-emergence of the same commitment (same dedupe_key) on a
    later session-end → skipped, store unchanged on that record."""
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.models import (
        CommitmentRecord, make_commitment_id, make_dedupe_key,
    )
    from mimir.turn_hooks import CommitmentExtractionHook

    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    # Pre-seed an active commitment.
    pre_existing = CommitmentRecord(
        id=make_commitment_id(),
        channel_id="poller:github-activity",
        text="Apply Item 7 fix for PR #111",
    )
    await store.add(pre_existing)
    existing_key = store.current_state()[pre_existing.id].dedupe_key

    # Mock extractor returns a "fresh" record with the same dedupe key
    # (what a re-emergence on the next session-end would look like).
    new_id = make_commitment_id()

    async def fake_extract(output, *, channel_id, saga_session_id, source_turn_id):
        rec = CommitmentRecord(
            id=new_id, channel_id="poller:github-activity",
            text="Apply Item 7 fix for PR #111",
            kind="agent_promise", confidence=0.9,
            source_turn_id=source_turn_id,
            saga_session_id=saga_session_id,
        )
        rec.dedupe_key = existing_key
        return [rec]

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", fake_extract,
    )

    ctx = _make_session_end_ctx(turn_id="t-later", saga="s-2")
    # Long enough to clear MIN_OUTPUT_LEN — the fake extractor below
    # ignores the content but the hook's short-output gate would skip
    # to the no-op path otherwise.
    record = _make_session_end_record("x" * 200, turn_id="t-later")
    await hook.finalize(ctx, _make_event(), record)

    state = store.current_state()
    # Still only the pre-existing record; the duplicate was skipped.
    assert len(state) == 1
    assert pre_existing.id in state
    assert new_id not in state


@pytest.mark.asyncio
async def test_commitment_extraction_swallows_extractor_failures(
    tmp_path, monkeypatch,
):
    """Extractor raising must NOT bubble up to break the synthesis
    turn's finalize. Log + return; the same commitments resurface on
    the next session-end if real."""
    from mimir.commitments import CommitmentsStore
    from mimir.turn_hooks import CommitmentExtractionHook

    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    async def raising_extract(*args, **kwargs):
        raise RuntimeError("LLM is angry")

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", raising_extract,
    )

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("x" * 200)
    # Must not raise.
    await hook.finalize(ctx, _make_event(), record)
    assert store.current_state() == {}


# ─── PR #125 review fixes ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commitment_extraction_emits_short_output_no_op(
    tmp_path, monkeypatch,
):
    """PR #125 review #6: short-output skip path now emits
    ``commitments_extraction_no_op`` with reason=short_output so
    backtest validation can tell it apart from llm_returned_zero."""
    import json
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.extractor import EXTRACTION_PROMPT_VERSION
    from mimir.event_logger import init_logger
    from mimir.turn_hooks import CommitmentExtractionHook

    # Autouse ``_logger`` fixture already initialized the logger.
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("too short")  # < 100 chars
    await hook.finalize(ctx, _make_event(), record)

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    no_ops = [e for e in events if e.get("type") == "commitments_extraction_no_op"]
    assert len(no_ops) == 1
    assert no_ops[0]["reason"] == "short_output"
    assert no_ops[0]["output_len"] == len("too short")
    assert no_ops[0]["prompt_version"] == EXTRACTION_PROMPT_VERSION


@pytest.mark.asyncio
async def test_commitment_extraction_emits_llm_returned_zero_no_op(
    tmp_path, monkeypatch,
):
    """LLM ran (output cleared MIN_OUTPUT_LEN) but returned [] →
    ``commitments_extraction_no_op`` with reason=llm_returned_zero."""
    import json
    from mimir.commitments import CommitmentsStore
    from mimir.event_logger import init_logger
    from mimir.turn_hooks import CommitmentExtractionHook

    # Autouse ``_logger`` fixture already initialized the logger.
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    async def empty_extract(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", empty_extract,
    )

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("x" * 200)
    await hook.finalize(ctx, _make_event(), record)

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    no_ops = [e for e in events if e.get("type") == "commitments_extraction_no_op"]
    assert len(no_ops) == 1
    assert no_ops[0]["reason"] == "llm_returned_zero"


@pytest.mark.asyncio
async def test_commitment_extraction_emits_all_dedupe_skipped_no_op(
    tmp_path, monkeypatch,
):
    """Extractor returned N records, all skipped on dedupe (common
    after Phase 3 surfacing surfaces commitments across sessions) →
    ``commitments_extraction_no_op`` with reason=all_dedupe_skipped."""
    import json
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.models import CommitmentRecord, make_commitment_id
    from mimir.event_logger import init_logger
    from mimir.turn_hooks import CommitmentExtractionHook

    # Autouse ``_logger`` fixture already initialized the logger.
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    # Pre-seed an active commitment whose dedupe key the fake
    # extractor's record will match.
    pre = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    pre_key = store.current_state()[pre.id].dedupe_key

    async def dupe_extract(*args, **kwargs):
        rec = CommitmentRecord(
            id=make_commitment_id(), channel_id="c1", text="X",
        )
        rec.dedupe_key = pre_key
        return [rec]

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", dupe_extract,
    )

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("x" * 200)
    await hook.finalize(ctx, _make_event(), record)

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    no_ops = [e for e in events if e.get("type") == "commitments_extraction_no_op"]
    assert len(no_ops) == 1
    assert no_ops[0]["reason"] == "all_dedupe_skipped"
    assert no_ops[0]["skipped_dedupe"] == 1


@pytest.mark.asyncio
async def test_commitments_extracted_event_carries_prompt_version(
    tmp_path, monkeypatch,
):
    """PR #125 review #1: ``commitments_extracted`` event payload
    includes ``prompt_version`` for backtest filtering."""
    import json
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.extractor import EXTRACTION_PROMPT_VERSION
    from mimir.commitments.models import CommitmentRecord, make_commitment_id
    from mimir.event_logger import init_logger
    from mimir.turn_hooks import CommitmentExtractionHook

    # Autouse ``_logger`` fixture already initialized the logger.
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    hook = CommitmentExtractionHook(store=store)

    async def fake_extract(*args, **kwargs):
        return [CommitmentRecord(
            id=make_commitment_id(), channel_id="c1",
            text="extracted thing",
        )]

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", fake_extract,
    )

    ctx = _make_session_end_ctx()
    record = _make_session_end_record("x" * 200)
    await hook.finalize(ctx, _make_event(), record)

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    extracted_events = [e for e in events if e.get("type") == "commitments_extracted"]
    assert len(extracted_events) == 1
    assert extracted_events[0]["prompt_version"] == EXTRACTION_PROMPT_VERSION
    assert extracted_events[0]["count"] == 1
