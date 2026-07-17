"""Commitments due-check poller (Phase 2b).

Emits ``commitment_due`` / ``commitment_expired`` /
``commitment_snooze_pileup`` algedonic events based on store state.
Tests pin the lifecycle transitions (deliver / expire), the dedupe
behavior (don't re-emit on every poll tick), and the events that
actually land in ``events.jsonl``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from mimir.commitments import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentStatus,
    CommitmentsStore,
    make_commitment_id,
)
from mimir.commitments.poller import (
    DEFAULT_SNOOZE_PILEUP_THRESHOLD,
    check_due_and_expired,
)
from mimir.event_logger import init_logger


def _events(home: Path) -> list[dict]:
    p = home / "logs" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Init event logger so the poller's ``log_event`` calls land in
    a deterministic file we can read for assertions."""
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-poller")
    return tmp_path


@pytest.mark.asyncio
async def test_current_state_read_runs_off_event_loop(tmp_path: Path):
    """chainlink #843: store replay can be size-scaled, so offload it."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    await store.add(CommitmentRecord(
        id=make_commitment_id(),
        channel_id="c1",
        text="Future reminder",
        due_window_start_unix=time.time() + 3600,
    ))

    loop_thread = threading.get_ident()
    observed_threads: list[int] = []
    real_current_state = store.current_state

    def wrapped_current_state():
        observed_threads.append(threading.get_ident())
        return real_current_state()

    store.current_state = wrapped_current_state  # type: ignore[method-assign]

    result = await check_due_and_expired(store)

    assert result.scanned == 1
    assert observed_threads, "current_state was not called"
    assert all(thread_id != loop_thread for thread_id in observed_threads), (
        "commitments current_state replay ran on the event-loop thread"
    )


@pytest.mark.asyncio
async def test_size_scaled_current_state_read_does_not_lag_event_loop(
    tmp_path: Path,
) -> None:
    """A large commitments log replay must not monopolize the event loop."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    for i in range(2_000):
        await store.add(CommitmentRecord(
            id=make_commitment_id(),
            channel_id="c1",
            text=f"Future reminder {i}",
            due_window_start_unix=time.time() + 3600,
        ))

    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    try:
        result = await asyncio.wait_for(check_due_and_expired(store), timeout=5)
    finally:
        done.set()
        await ticker_task

    assert result.scanned == 2_000
    assert ticks > 0, "event loop did not advance while current_state replay ran"


@pytest.mark.asyncio
async def test_skips_terminal_records(tmp_path: Path, home: Path):
    """Completed / dismissed / expired records must not surface any
    new events. Replay's VALID_TRANSITIONS would reject the deliver
    anyway, but the poller filters first."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=time.time() - 86400,  # past
        due_window_end_unix=time.time() + 86400,
    ))
    await store.complete(rec.id)

    result = await check_due_and_expired(store)
    assert result.due_emitted == 0
    assert result.expired_emitted == 0
    # No commitment_due event in events.jsonl.
    types = {e.get("type") for e in _events(home)}
    assert "commitment_due" not in types


@pytest.mark.asyncio
async def test_emits_due_when_window_open(tmp_path: Path, home: Path):
    """now ∈ [start, end] AND status=pending → fire commitment_due,
    mark delivered."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="chan-1",
        text="Review PR #111", kind=CommitmentKind.AGENT_PROMISE.value,
        recipient_identity="alice",
        due_window_start_unix=now - 60,  # just-opened
        due_window_end_unix=now + 86400,
    ))

    result = await check_due_and_expired(store, now_unix=now)
    assert result.due_emitted == 1
    assert result.expired_emitted == 0

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.DELIVERED.value
    assert state[rec.id].attempts == 1

    due_events = [e for e in _events(home) if e.get("type") == "commitment_due"]
    assert len(due_events) == 1
    assert due_events[0]["commitment_id"] == rec.id
    assert due_events[0]["text"] == "Review PR #111"
    assert due_events[0]["recipient_identity"] == "alice"


@pytest.mark.asyncio
async def test_skips_pending_not_yet_due(tmp_path: Path, home: Path):
    """now < start → skip, no event, no state change."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=now + 3600,  # 1h from now
        due_window_end_unix=now + 86400,
    ))

    result = await check_due_and_expired(store, now_unix=now)
    assert result.due_emitted == 0
    assert result.skipped_not_yet_due == 1
    assert store.current_state()[rec.id].status == CommitmentStatus.PENDING.value


@pytest.mark.asyncio
async def test_does_not_re_emit_due_on_already_delivered(
    tmp_path: Path, home: Path,
):
    """Once a commitment has been delivered, subsequent poll ticks
    must NOT re-emit commitment_due. The deliver-bump-on-each-tick
    pattern would flood events.jsonl over multi-day windows."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=now - 60,
        due_window_end_unix=now + 86400,
    ))

    # First sweep: fires due.
    r1 = await check_due_and_expired(store, now_unix=now)
    assert r1.due_emitted == 1

    # Second sweep (same now): should NOT re-fire.
    r2 = await check_due_and_expired(store, now_unix=now + 5)
    assert r2.due_emitted == 0


@pytest.mark.asyncio
async def test_emits_expired_when_window_ends(tmp_path: Path, home: Path):
    """now > end AND not terminal → fire commitment_expired, mark
    expired. Works on pending, delivered, AND snoozed records."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    base = time.time()
    end = base - 60
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=base - 86400,
        due_window_end_unix=end,
    ))

    result = await check_due_and_expired(store, now_unix=base)
    assert result.expired_emitted == 1
    assert result.due_emitted == 0  # didn't fire due — expired path wins

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.EXPIRED.value

    expired_events = [
        e for e in _events(home) if e.get("type") == "commitment_expired"
    ]
    assert len(expired_events) == 1
    assert expired_events[0]["commitment_id"] == rec.id


@pytest.mark.asyncio
async def test_expired_after_delivered_still_fires(
    tmp_path: Path, home: Path,
):
    """A commitment that was delivered (due) but never completed →
    expired path fires when window ends. Common path: agent saw the
    reminder, didn't act, window elapsed."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=now - 86400,
        due_window_end_unix=now + 60,
    ))
    # First sweep delivers.
    await check_due_and_expired(store, now_unix=now)
    # Time advances past end.
    result = await check_due_and_expired(store, now_unix=now + 120)
    assert result.expired_emitted == 1
    assert store.current_state()[rec.id].status == CommitmentStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_open_ended_commitments_skipped(tmp_path: Path, home: Path):
    """No due_window_start → can't fire due/expired (no time anchor).
    Counted under skipped_no_due_window. Phase 3 prompt block surfaces
    these instead."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="open-ended",
        due_window_start_unix=None,
        due_window_end_unix=None,
    ))
    result = await check_due_and_expired(store)
    assert result.due_emitted == 0
    assert result.expired_emitted == 0
    assert result.skipped_no_due_window == 1


@pytest.mark.asyncio
async def test_snoozed_respects_new_window(tmp_path: Path, home: Path):
    """Snooze slides due_window_start; the same "now ≥ start" check
    naturally respects the snooze. No special-case needed."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=now - 60,  # would fire now
        due_window_end_unix=now + 86400,
    ))
    # Snooze 1 day out.
    await store.snooze(rec.id, until_unix=now + 86400)

    # Poll now — should NOT fire (snooze pushed start out).
    result = await check_due_and_expired(store, now_unix=now)
    assert result.due_emitted == 0
    assert result.skipped_not_yet_due == 1
    assert store.current_state()[rec.id].status == CommitmentStatus.SNOOZED.value


# ─── Snooze pileup ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snooze_count_increments_on_each_snooze(tmp_path: Path):
    """The snooze_count field is incremented by replay each time the
    record is snoozed — used by the poller's pileup detection."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for i in range(4):
        await store.snooze(rec.id, until_unix=time.time() + (i + 1) * 86400)
    state = store.current_state()
    assert state[rec.id].snooze_count == 4


@pytest.mark.asyncio
async def test_pileup_emits_above_threshold(tmp_path: Path, home: Path):
    """snooze_count >= threshold → fire commitment_snooze_pileup with
    the count + threshold metadata."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="punted thing",
    ))
    for i in range(3):
        await store.snooze(rec.id, until_unix=time.time() + (i + 1) * 86400)

    result = await check_due_and_expired(store, snooze_pileup_threshold=3)
    assert result.snooze_pileup_emitted == 1

    pileups = [
        e for e in _events(home) if e.get("type") == "commitment_snooze_pileup"
    ]
    assert len(pileups) == 1
    assert pileups[0]["commitment_id"] == rec.id
    assert pileups[0]["snooze_count"] == 3
    assert pileups[0]["threshold"] == 3
    assert pileups[0]["text"] == "punted thing"


@pytest.mark.asyncio
async def test_pileup_below_threshold_silent(tmp_path: Path, home: Path):
    """snooze_count below threshold → no pileup event."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.snooze(rec.id, until_unix=time.time() + 86400)
    await store.snooze(rec.id, until_unix=time.time() + 2 * 86400)
    # snooze_count = 2, threshold = 3 → no pileup.
    result = await check_due_and_expired(store, snooze_pileup_threshold=3)
    assert result.snooze_pileup_emitted == 0


@pytest.mark.asyncio
async def test_pileup_terminal_records_excluded(tmp_path: Path, home: Path):
    """A commitment that was snoozed many times and THEN
    completed/dismissed shouldn't generate ongoing pileup signals."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for i in range(5):
        await store.snooze(rec.id, until_unix=time.time() + (i + 1) * 86400)
    await store.complete(rec.id)

    result = await check_due_and_expired(store, snooze_pileup_threshold=3)
    assert result.snooze_pileup_emitted == 0


@pytest.mark.asyncio
async def test_pileup_threshold_is_configurable(tmp_path: Path, home: Path):
    """The threshold param controls the trigger point."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.snooze(rec.id, until_unix=time.time() + 86400)
    # threshold=1 → fires.
    r1 = await check_due_and_expired(store, snooze_pileup_threshold=1)
    assert r1.snooze_pileup_emitted == 1
    # threshold=10 → no fire.
    r10 = await check_due_and_expired(store, snooze_pileup_threshold=10)
    assert r10.snooze_pileup_emitted == 0


@pytest.mark.asyncio
async def test_default_threshold_constant():
    """The default threshold is exposed for the agent wiring + docs."""
    assert DEFAULT_SNOOZE_PILEUP_THRESHOLD == 3


# ─── PR #126 review #2: pileup cooldown ─────────────────────────────


@pytest.mark.asyncio
async def test_pileup_cooldown_suppresses_within_24h(tmp_path: Path, home: Path):
    """PR #126 review #2: after a pileup alarm fires, subsequent
    poll ticks within 24h must NOT re-emit. Otherwise events.jsonl
    accrues a fresh row every 5 min (6k+ rows/week/chronic)."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="punted thing",
    ))
    for i in range(3):
        await store.snooze(rec.id, until_unix=now + (i + 1) * 86400)

    # First sweep: fires the alarm.
    r1 = await check_due_and_expired(
        store, now_unix=now, snooze_pileup_threshold=3,
    )
    assert r1.snooze_pileup_emitted == 1
    assert r1.snooze_pileup_suppressed_cooldown == 0

    # Second sweep, 1h later: must NOT re-fire (still in 24h window).
    r2 = await check_due_and_expired(
        store, now_unix=now + 3600, snooze_pileup_threshold=3,
    )
    assert r2.snooze_pileup_emitted == 0
    assert r2.snooze_pileup_suppressed_cooldown == 1

    # And the events.jsonl has exactly one pileup row, not two.
    pileups = [
        e for e in _events(home) if e.get("type") == "commitment_snooze_pileup"
    ]
    assert len(pileups) == 1


@pytest.mark.asyncio
async def test_pileup_re_emits_after_24h(tmp_path: Path, home: Path):
    """After the 24h cooldown elapses, the next pileup-detected sweep
    re-emits — the agent still hasn't acted, surface again."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for _ in range(3):
        await store.snooze(rec.id, until_unix=now + 86400)

    r1 = await check_due_and_expired(
        store, now_unix=now, snooze_pileup_threshold=3,
    )
    assert r1.snooze_pileup_emitted == 1

    # 25h later — cooldown elapsed.
    r2 = await check_due_and_expired(
        store, now_unix=now + 25 * 3600, snooze_pileup_threshold=3,
    )
    assert r2.snooze_pileup_emitted == 1
    assert r2.snooze_pileup_suppressed_cooldown == 0


@pytest.mark.asyncio
async def test_pileup_alarm_sets_record_field(tmp_path: Path, home: Path):
    """The alarm writes a ``commitment_pileup_alarmed`` event that
    replay translates to ``rec.pileup_alarmed_at_unix``. The record's
    status is unchanged (annotational, not a transition)."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for _ in range(3):
        await store.snooze(rec.id, until_unix=now + 86400)

    # Pre-alarm: field is None.
    assert store.current_state()[rec.id].pileup_alarmed_at_unix is None
    pre_status = store.current_state()[rec.id].status

    await check_due_and_expired(
        store, now_unix=now, snooze_pileup_threshold=3,
    )

    state = store.current_state()
    # Field bumped.
    assert state[rec.id].pileup_alarmed_at_unix is not None
    assert state[rec.id].pileup_alarmed_at_unix > 0
    # Status NOT changed (still SNOOZED from the last snooze).
    assert state[rec.id].status == pre_status


@pytest.mark.asyncio
async def test_store_alarm_pileup_unknown_id(tmp_path: Path):
    """alarm_pileup on a nonexistent id returns False; no events written."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    assert await store.alarm_pileup("c-nonexistent") is False


@pytest.mark.asyncio
async def test_emit_at_exact_end_uses_due_branch(tmp_path: Path, home: Path):
    """PR #126 review nit on `>` strict inequality: a commitment
    whose ``end == now_unix`` is treated as still-in-window — fires
    ``commitment_due``, not ``commitment_expired``. Pins the boundary
    behavior so a future refactor can't accidentally flip it."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="boundary",
        due_window_start_unix=now - 86400,
        due_window_end_unix=now,  # exactly equal to now
    ))
    result = await check_due_and_expired(store, now_unix=now)
    # Due fires, not expired.
    assert result.due_emitted == 1
    assert result.expired_emitted == 0
    assert store.current_state()[rec.id].status == CommitmentStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_pileup_alarm_runs_before_log_event(tmp_path: Path, home: Path):
    """PR #126 re-review observation: ``alarm_pileup`` must complete
    BEFORE ``log_event`` so a failure on either side never leaves the
    pair inconsistent in the way that produces a duplicate algedonic
    row. We can't easily inject a real file-append failure on
    events.jsonl mid-test, but we can monkeypatch the ordering and
    assert alarm was called first."""
    from mimir.commitments import poller as poller_mod

    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for _ in range(3):
        await store.snooze(rec.id, until_unix=now + 86400)

    call_order: list[str] = []
    original_alarm = store.alarm_pileup
    original_log = poller_mod.log_event

    async def tracking_alarm(id: str) -> bool:
        call_order.append("alarm")
        return await original_alarm(id)

    async def tracking_log(event_type: str, **payload):
        call_order.append("log")
        return await original_log(event_type, **payload)

    store.alarm_pileup = tracking_alarm  # type: ignore[method-assign]
    poller_mod.log_event = tracking_log  # type: ignore[assignment]
    try:
        result = await check_due_and_expired(
            store, now_unix=now, snooze_pileup_threshold=3,
        )
    finally:
        poller_mod.log_event = original_log  # type: ignore[assignment]

    assert call_order == ["alarm", "log"], (
        f"expected alarm before log_event, got {call_order}. "
        "Reversed order would leave a duplicate row on log_event "
        "failure-then-recover; alarm-first means we miss one "
        "surfacing round instead of duplicating."
    )
    # PR #132 review nit: pin that the counter increments after the
    # await pair so a future reorder of the increment relative to the
    # awaits gets caught.
    assert result.snooze_pileup_emitted == 1


@pytest.mark.asyncio
async def test_pileup_alarm_failure_skips_log_event(tmp_path: Path, home: Path):
    """When ``alarm_pileup`` raises, the algedonic emit must NOT fire
    (we'd otherwise write events.jsonl with no cooldown marker,
    re-firing on the next tick). The try/except catches the alarm
    raise; counter stays 0."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    for _ in range(3):
        await store.snooze(rec.id, until_unix=now + 86400)

    log_called = False
    from mimir.commitments import poller as poller_mod
    original_log = poller_mod.log_event

    async def tracking_log(event_type: str, **payload):
        nonlocal log_called
        if event_type == "commitment_snooze_pileup":
            log_called = True
        return await original_log(event_type, **payload)

    async def failing_alarm(id: str) -> bool:
        raise OSError("disk full")

    store.alarm_pileup = failing_alarm  # type: ignore[method-assign]
    poller_mod.log_event = tracking_log  # type: ignore[assignment]
    try:
        result = await check_due_and_expired(
            store, now_unix=now, snooze_pileup_threshold=3,
        )
    finally:
        poller_mod.log_event = original_log  # type: ignore[assignment]

    assert log_called is False, (
        "alarm_pileup raised → log_event must not fire (would leave "
        "an algedonic row without a cooldown marker)."
    )
    assert result.snooze_pileup_emitted == 0
