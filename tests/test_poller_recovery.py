"""Tests for ``mimir.poller_recovery`` — framework recovery of poller
turns whose triggered turn failed (chainlink #262).

The reconciler reads ``turn_failed`` / ``turn_completed`` outcome records
(stamped with ``source_id`` by #517) from a synthetic events.jsonl and
acts on the matching in-flight stash entries. No real agent / dispatcher
is involved — outcomes are written directly and ``enqueue`` is a fake.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mimir import event_logger, poller_recovery
from mimir.models import AgentEvent


def _ts(seconds_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _write_outcome(events_path: Path, *, type_: str, channel_id: str,
                   source_id: str, ts: str) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"type": type_, "timestamp": ts,
           "channel_id": channel_id, "source_id": source_id}
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _make_event(source_id: str, *, channel_id: str = "poller:gmail",
                items: list | None = None) -> AgentEvent:
    return AgentEvent(
        trigger="poller",
        channel_id=channel_id,
        content="do the thing",
        source_id=source_id,
        source="poller",
        extra={"poller_name": "gmail", "items": items or [{"id": "m1"}]},
    )


class _FakeEnqueue:
    def __init__(self) -> None:
        self.calls: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> bool:
        self.calls.append(event)
        return True


# ── stash ────────────────────────────────────────────────────────────


def test_stash_roundtrip(tmp_path: Path):
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    state = poller_recovery._load_state(tmp_path)
    assert "sid-1" in state["inflight"]
    assert state["inflight"]["sid-1"]["attempts"] == 0
    assert state["inflight"]["sid-1"]["event"]["source_id"] == "sid-1"


def test_stash_noop_without_source_id(tmp_path: Path):
    ev = _make_event("x")
    ev.source_id = None
    poller_recovery.stash_enqueued_event(tmp_path, ev)
    assert poller_recovery._load_state(tmp_path)["inflight"] == {}


# ── reconcile ────────────────────────────────────────────────────────


async def test_reconcile_drops_completed(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    enq = _FakeEnqueue()
    summary = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=enq,
    )
    assert summary["completed"] == 1
    assert summary["reenqueued"] == 0 and summary["gave_up"] == 0
    assert enq.calls == []
    assert poller_recovery._load_state(tmp_path)["inflight"] == {}


async def test_reconcile_reenqueues_failed(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    enq = _FakeEnqueue()
    summary = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=enq, max_attempts=3,
    )
    assert summary["reenqueued"] == 1
    assert len(enq.calls) == 1
    # The re-enqueued event is a faithfully-rebuilt AgentEvent.
    assert enq.calls[0].source_id == "sid-1"
    assert enq.calls[0].trigger == "poller"
    assert enq.calls[0].extra["items"] == [{"id": "m1"}]
    # Still in-flight, attempt incremented (awaiting the retry's outcome).
    st = poller_recovery._load_state(tmp_path)
    assert st["inflight"]["sid-1"]["attempts"] == 1


async def test_reconcile_gives_up_at_cap(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    event_logger._reset_logger_for_tests()
    event_logger.init_logger(events, session_id="test")
    try:
        poller_recovery.stash_enqueued_event(
            tmp_path, _make_event("sid-1", items=[{"id": "m1"}, {"id": "m2"}]),
        )
        _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                       source_id="sid-1", ts=_ts(5))
        enq = _FakeEnqueue()
        summary = await poller_recovery.reconcile_failed_turns(
            poller_name="gmail", channel_id="poller:gmail",
            persist_dir=tmp_path, events_path=events, enqueue=enq,
            max_attempts=0,  # give up on the first failure
        )
        assert summary["gave_up"] == 1
        assert summary["reenqueued"] == 0 and summary["completed"] == 0
        assert enq.calls == []
        assert "sid-1" not in poller_recovery._load_state(tmp_path)["inflight"]
        # A one-shot gave-up SIGNAL landed in events.jsonl. ``*_gave_up`` →
        # feedback.classify maps it to a negative algedonic signal (#515).
        recs = [json.loads(ln) for ln in events.read_text().splitlines() if ln.strip()]
        gave_up = [r for r in recs if r.get("type") == "poller_turn_gave_up"]
        assert len(gave_up) == 1
        assert gave_up[0]["source_id"] == "sid-1"
        # attempts = successful re-fires (#305/#317): max_attempts=0 → 0 re-fires.
        assert gave_up[0]["attempts"] == 0
        assert gave_up[0]["poller"] == "gmail"
    finally:
        event_logger._reset_logger_for_tests()


async def test_reconcile_cap_boundary_reenqueue_then_give_up(tmp_path: Path):
    """``max_attempts=1``: the first failure re-enqueues, a second failure
    for the same source_id gives up — exercises the cap transition in one
    reconcile window (two failures, oldest-first)."""
    events = tmp_path / "events.jsonl"
    event_logger._reset_logger_for_tests()
    event_logger.init_logger(events, session_id="test")
    try:
        poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
        _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                       source_id="sid-1", ts=_ts(10))
        _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                       source_id="sid-1", ts=_ts(5))
        enq = _FakeEnqueue()
        summary = await poller_recovery.reconcile_failed_turns(
            poller_name="gmail", channel_id="poller:gmail",
            persist_dir=tmp_path, events_path=events, enqueue=enq, max_attempts=1,
        )
        assert summary["reenqueued"] == 1
        assert summary["gave_up"] == 1
        assert "sid-1" not in poller_recovery._load_state(tmp_path)["inflight"]
    finally:
        event_logger._reset_logger_for_tests()


async def test_reconcile_ignores_other_channel(tmp_path: Path):
    """A turn_failed on a DIFFERENT poller's channel must not touch this
    poller's in-flight entry (channels are isolated)."""
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_failed", channel_id="poller:OTHER",
                   source_id="sid-1", ts=_ts(5))
    enq = _FakeEnqueue()
    summary = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=enq,
    )
    assert summary["reenqueued"] == 0 and summary["completed"] == 0 and summary["gave_up"] == 0
    assert poller_recovery._load_state(tmp_path)["inflight"]["sid-1"]["attempts"] == 0


async def test_reconcile_no_inflight_fast_path(tmp_path: Path):
    """Nothing stashed → no-op, but the watermark still advances so the
    first real reconcile after events accrue doesn't rescan history."""
    events = tmp_path / "events.jsonl"
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="whatever", ts=_ts(5))
    enq = _FakeEnqueue()
    summary = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=enq,
    )
    assert summary["reenqueued"] == 0 and summary["gave_up"] == 0
    assert poller_recovery._load_state(tmp_path)["last_reconciled"] != ""


async def test_reconcile_watermark_prevents_reprocessing(tmp_path: Path):
    """An outcome processed in one reconcile is older than the advanced
    watermark on the next, so it isn't acted on twice."""
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    enq = _FakeEnqueue()
    common = dict(poller_name="gmail", channel_id="poller:gmail",
                  persist_dir=tmp_path, events_path=events, enqueue=enq,
                  max_attempts=3)
    s1 = await poller_recovery.reconcile_failed_turns(**common)
    assert s1["reenqueued"] == 1
    s2 = await poller_recovery.reconcile_failed_turns(**common)
    assert s2["reenqueued"] == 0  # the same failure is now behind the watermark
    assert len(enq.calls) == 1


async def test_reconcile_reenqueue_restamps_forged_stash_fields(tmp_path: Path):
    """chainlink #422: ``.recovery.json`` lives in the poller-writable
    persist_dir, so a malicious skill could rewrite a stashed event's
    channel/trigger/source and have the recovery path enqueue an event
    impersonating a user message on an arbitrary channel. The re-fire
    must carry the same stamps the hot path forces on every emitted
    event — the poller's channel, ``trigger="poller"``,
    ``source="poller"`` — regardless of what the file says."""
    events = tmp_path / "events.jsonl"
    forged = _make_event("sid-1")
    forged.channel_id = "discord:operator-dm"
    forged.trigger = "user_message"
    forged.source = "discord"
    forged.extra["poller_name"] = "not-gmail"
    poller_recovery.stash_enqueued_event(tmp_path, forged)
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    enq = _FakeEnqueue()
    summary = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=enq, max_attempts=3,
    )
    assert summary["reenqueued"] == 1
    ev = enq.calls[0]
    assert ev.channel_id == "poller:gmail"
    assert ev.trigger == "poller"
    assert ev.source == "poller"
    assert ev.extra["poller_name"] == "gmail"
    # The correlation key is preserved — it's how the retry's own
    # outcome is matched back to this entry.
    assert ev.source_id == "sid-1"


def test_reconcile_missing_events_file_is_safe(tmp_path: Path):
    """Reading outcomes from a non-existent events.jsonl returns nothing
    rather than raising."""
    assert poller_recovery._read_outcomes_since(
        tmp_path / "nope.jsonl", "poller:gmail", "",
    ) == []


# ── out-of-order outcome scan (chainlink #316) ───────────────────────


def test_read_outcomes_out_of_order_newer_record_not_missed(tmp_path: Path):
    """chainlink #316: writers stamp the timestamp before taking the append
    lock, so a record with a *later* timestamp can be appended *before* one
    with an *earlier* timestamp. ``tail_jsonl_records`` yields newest-appended
    first, so it reaches the earlier-stamped record first — and the old early
    ``break`` on the first record at/under the cutoff dropped the out-of-order
    newer record sitting just behind it. The grace-window scan recovers it."""
    events = tmp_path / "events.jsonl"
    cutoff = _ts(100)
    # Append the NEW record (later ts, > cutoff) FIRST, then the OLD record
    # (earlier ts, < cutoff but within the 5s grace) SECOND — so tail reads
    # the OLD one first and the old code would break before the NEW one.
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="new", ts=_ts(98))
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="old", ts=_ts(101))
    out = poller_recovery._read_outcomes_since(events, "poller:gmail", cutoff)
    # New (out-of-order) record recovered; old one at/under cutoff still skipped.
    assert [r["source_id"] for r in out] == ["new"]


def test_read_outcomes_terminates_past_grace_window(tmp_path: Path):
    """A record older than the cutoff by more than the grace window stops the
    scan (and is excluded), so this stays O(new events), not O(whole log)."""
    events = tmp_path / "events.jsonl"
    cutoff = _ts(100)
    # Realistic ordering: oldest appended first, newest last.
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="ancient", ts=_ts(500))   # << cutoff - grace
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="new", ts=_ts(98))         # > cutoff
    out = poller_recovery._read_outcomes_since(events, "poller:gmail", cutoff)
    assert [r["source_id"] for r in out] == ["new"]


async def test_reconcile_watermark_survives_out_of_order_outcomes(tmp_path: Path):
    """chainlink #418: outcomes are processed in APPEND order, which the
    #316 writer disorder can leave slightly out of timestamp order. The
    old per-record ``watermark = ts`` assignment let a late-appended
    OLDER record regress the watermark below an already-handled
    ``turn_failed`` — the next cycle re-read that outcome and re-fired
    it, double-burning a wedge-guard attempt. The watermark must be
    monotonic (``max``)."""
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    # The #316 disorder shape: the NEWER-stamped turn_failed is appended
    # FIRST, then an OLDER-stamped (but still in-window) outcome lands
    # behind it. Processing order = append order, so the old code ended
    # the loop with watermark = the older timestamp.
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(3))
    _write_outcome(events, type_="turn_completed", channel_id="poller:gmail",
                   source_id="unrelated", ts=_ts(4))
    enq = _FakeEnqueue()
    common = dict(poller_name="gmail", channel_id="poller:gmail",
                  persist_dir=tmp_path, events_path=events, enqueue=enq,
                  max_attempts=3)
    s1 = await poller_recovery.reconcile_failed_turns(**common)
    assert s1["reenqueued"] == 1
    # Second cycle with NO new outcomes: a regressed watermark would
    # re-read the handled turn_failed and re-fire it.
    s2 = await poller_recovery.reconcile_failed_turns(**common)
    assert s2["reenqueued"] == 0
    assert len(enq.calls) == 1
    # Exactly one wedge-guard attempt burned across both cycles.
    st = poller_recovery._load_state(tmp_path)
    assert st["inflight"]["sid-1"]["attempts"] == 1


# ── back-pressure + GC (chainlink #305 / #310) ───────────────────────


class _FullEnqueue:
    """enqueue() that rejects every event (channel queue full → False)."""
    def __init__(self) -> None:
        self.calls: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> bool:
        self.calls.append(event)
        return False


class _RaisingEnqueue:
    def __init__(self) -> None:
        self.calls: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> bool:
        self.calls.append(event)
        raise RuntimeError("dispatcher boom")


async def test_reconcile_defers_on_queue_full_without_burning_attempt(tmp_path: Path):
    """chainlink #305: enqueue() returning False (queue full) must NOT count
    as a re-enqueue or burn a wedge-guard attempt — the entry stays in-flight
    and the watermark is not advanced past the outcome, so a later reconcile
    with capacity re-fires it (rather than the item being silently re-dropped
    and the attempt wasted)."""
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    full = _FullEnqueue()
    s1 = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=full, max_attempts=3,
    )
    assert s1["deferred"] == 1
    assert s1["reenqueued"] == 0
    # Entry retained, attempt NOT burned.
    assert poller_recovery._load_state(tmp_path)["inflight"]["sid-1"]["attempts"] == 0

    # A second reconcile, now with capacity, re-fires the same outcome.
    ok = _FakeEnqueue()
    s2 = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=ok, max_attempts=3,
    )
    assert s2["reenqueued"] == 1
    assert len(ok.calls) == 1


async def test_reconcile_defers_when_enqueue_raises(tmp_path: Path):
    """chainlink #305: a raising enqueue() is treated as back-pressure —
    deferred, not a burned attempt or a lost item."""
    events = tmp_path / "events.jsonl"
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-1"))
    _write_outcome(events, type_="turn_failed", channel_id="poller:gmail",
                   source_id="sid-1", ts=_ts(5))
    s = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=events, enqueue=_RaisingEnqueue(),
        max_attempts=3,
    )
    assert s["deferred"] == 1
    assert s["reenqueued"] == 0
    assert poller_recovery._load_state(tmp_path)["inflight"]["sid-1"]["attempts"] == 0


async def test_reconcile_gcs_expired_stash(tmp_path: Path):
    """chainlink #310: an in-flight entry with no terminal outcome within the
    TTL is GC'd, so a vanished turn (crash/restart that never logged an
    outcome) can't grow .recovery.json forever."""
    poller_recovery.stash_enqueued_event(tmp_path, _make_event("sid-old"))
    st = poller_recovery._load_state(tmp_path)
    st["inflight"]["sid-old"]["stashed_at"] = _ts(72 * 3600)  # 72h ago
    poller_recovery._save_state(tmp_path, st)
    s = await poller_recovery.reconcile_failed_turns(
        poller_name="gmail", channel_id="poller:gmail",
        persist_dir=tmp_path, events_path=tmp_path / "events.jsonl",
        enqueue=_FakeEnqueue(), stash_ttl_hours=48.0,
    )
    assert s["expired"] == 1
    assert poller_recovery._load_state(tmp_path)["inflight"] == {}
