"""CommitmentsStore — append-only lifecycle + replay + status-aware trim."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mimir.commitments import (
    CommitmentKind,
    CommitmentRecord,
    CommitmentSensitivity,
    CommitmentStatus,
    CommitmentsStore,
    make_commitment_id,
    make_dedupe_key,
)


# ─── make_dedupe_key ────────────────────────────────────────────────


def test_dedupe_key_stable_for_same_inputs():
    k1 = make_dedupe_key(channel_id="c1", text="Review PR", due_window_start_unix=None)
    k2 = make_dedupe_key(channel_id="c1", text="Review PR", due_window_start_unix=None)
    assert k1 == k2


def test_dedupe_key_normalizes_whitespace_and_case():
    k1 = make_dedupe_key(channel_id="c1", text="Review PR", due_window_start_unix=None)
    k2 = make_dedupe_key(channel_id="c1", text="  REVIEW   pr  ", due_window_start_unix=None)
    assert k1 == k2


def test_dedupe_key_distinguishes_channels():
    k1 = make_dedupe_key(channel_id="c1", text="Review PR", due_window_start_unix=None)
    k2 = make_dedupe_key(channel_id="c2", text="Review PR", due_window_start_unix=None)
    assert k1 != k2


def test_dedupe_key_buckets_by_day_not_second():
    """Same due day → same key. Different days → different keys."""
    base = 1_715_000_000.0  # arbitrary unix
    k1 = make_dedupe_key(channel_id="c1", text="X", due_window_start_unix=base)
    k2 = make_dedupe_key(channel_id="c1", text="X", due_window_start_unix=base + 3600)
    k3 = make_dedupe_key(channel_id="c1", text="X", due_window_start_unix=base + 86400 * 2)
    assert k1 == k2  # same day
    assert k1 != k3  # different day


def test_dedupe_key_none_due_bucket_is_consistent():
    k1 = make_dedupe_key(channel_id="c1", text="X", due_window_start_unix=None)
    k2 = make_dedupe_key(channel_id="c1", text="X", due_window_start_unix=None)
    assert k1 == k2


# ─── Basic add + replay ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_and_replay_round_trips(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "commitments.jsonl")
    rec = CommitmentRecord(
        id=make_commitment_id(),
        channel_id="chan-1",
        text="Review PR #111",
        kind=CommitmentKind.AGENT_PROMISE.value,
        suggested_reminder="PR #111 awaiting review",
    )
    saved = await store.add(rec)

    state = store.current_state()
    assert saved.id in state
    out = state[saved.id]
    assert out.text == "Review PR #111"
    assert out.status == CommitmentStatus.PENDING.value
    assert out.kind == CommitmentKind.AGENT_PROMISE.value
    assert out.dedupe_key  # auto-filled
    assert out.created_at_unix > 0  # auto-filled


@pytest.mark.asyncio
async def test_add_coerces_initial_status_to_pending(tmp_path: Path):
    """A caller copy-pasting status=completed on add() must NOT poison
    the store — the contract is that add() starts pending."""
    store = CommitmentsStore(path=tmp_path / "commitments.jsonl")
    rec = CommitmentRecord(
        id=make_commitment_id(),
        channel_id="chan-1",
        text="X",
        status=CommitmentStatus.COMPLETED.value,  # wrong; should be coerced
    )
    saved = await store.add(rec)
    assert saved.status == CommitmentStatus.PENDING.value
    state = store.current_state()
    assert state[saved.id].status == CommitmentStatus.PENDING.value


# ─── Lifecycle transitions ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_bumps_attempts_and_sets_status(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.deliver(rec.id)
    await store.deliver(rec.id)

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.DELIVERED.value
    assert state[rec.id].attempts == 2


@pytest.mark.asyncio
async def test_complete_is_terminal(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.complete(rec.id, message_id="m-42")

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.COMPLETED.value
    assert state[rec.id].completion_message_id == "m-42"
    assert state[rec.id].is_terminal()


@pytest.mark.asyncio
async def test_snooze_slides_due_window_start(tmp_path: Path):
    """Snoozing should push the due_window_start so surfacing logic
    knows the next earliest-deliver anchor."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    original_start = 1_000_000.0
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
        due_window_start_unix=original_start,
        due_window_end_unix=original_start + 86400,
    ))
    new_start = original_start + 7 * 86400
    await store.snooze(rec.id, until_unix=new_start, reason="not yet")

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.SNOOZED.value
    assert state[rec.id].snoozed_until_unix == new_start
    assert state[rec.id].due_window_start_unix == new_start
    assert state[rec.id].snooze_reason == "not yet"


@pytest.mark.asyncio
async def test_dismiss_with_reason(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.dismiss(rec.id, reason="no longer relevant")

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.DISMISSED.value
    assert state[rec.id].dismiss_reason == "no longer relevant"
    assert state[rec.id].is_terminal()


@pytest.mark.asyncio
async def test_expire(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    await store.expire(rec.id)

    state = store.current_state()
    assert state[rec.id].status == CommitmentStatus.EXPIRED.value
    assert state[rec.id].is_terminal()


@pytest.mark.asyncio
async def test_lifecycle_event_for_unknown_id_is_skipped(tmp_path: Path):
    """Replay must tolerate lifecycle events whose add was trimmed
    away — log + skip, not crash."""
    path = tmp_path / "c.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a delivered event without a preceding add.
    with path.open("w") as f:
        f.write(json.dumps({
            "type": "commitment_delivered",
            "id": "c-orphan",
            "at_unix": time.time(),
        }) + "\n")
    store = CommitmentsStore(path=path)
    state = store.current_state()
    assert state == {}  # nothing created, no crash


# ─── List filters ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters_by_channel(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="ch1", text="X1"))
    await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="ch2", text="X2"))
    await store.add(CommitmentRecord(id=make_commitment_id(), channel_id=None, text="X3-unbound"))

    rows_ch1 = store.list(channel_id="ch1")
    # ch1-bound + unbound (per design rule "unbound surfaces everywhere")
    assert {r.text for r in rows_ch1} == {"X1", "X3-unbound"}

    rows_ch1_strict = store.list(channel_id="ch1", include_unbound=False)
    assert {r.text for r in rows_ch1_strict} == {"X1"}


@pytest.mark.asyncio
async def test_list_filters_by_status(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    r1 = await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="c1", text="X1"))
    r2 = await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="c1", text="X2"))
    await store.complete(r1.id)

    pending = store.list(status=CommitmentStatus.PENDING.value)
    completed = store.list(status=CommitmentStatus.COMPLETED.value)
    assert {r.text for r in pending} == {"X2"}
    assert {r.text for r in completed} == {"X1"}


@pytest.mark.asyncio
async def test_list_sorted_by_created_at(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    base = time.time()
    r1 = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="first",
        created_at_unix=base,
    ))
    r2 = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="second",
        created_at_unix=base + 100,
    ))
    r3 = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="third",
        created_at_unix=base + 50,
    ))
    rows = store.list()
    assert [r.text for r in rows] == ["first", "third", "second"]


# ─── find_by_dedupe_key ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_by_dedupe_key_returns_active(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="Review PR #1",
    ))
    found = store.find_by_dedupe_key(rec.dedupe_key)
    assert found is not None
    assert found.id == rec.id


@pytest.mark.asyncio
async def test_find_by_dedupe_key_ignores_terminal(tmp_path: Path):
    """A completed/dismissed commitment shouldn't dedupe a fresh extraction
    — the new one is a re-emergence, treat as new."""
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="Review PR #1",
    ))
    await store.complete(rec.id)
    assert store.find_by_dedupe_key(rec.dedupe_key) is None


# ─── Status-aware trim ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trim_drops_terminal_records_older_than_retention(tmp_path: Path):
    store = CommitmentsStore(
        path=tmp_path / "c.jsonl",
        terminal_retention_days=30,
    )
    now = time.time()
    # Old completed record (40 days ago) — should drop.
    r_old = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="old",
        created_at_unix=now - 40 * 86400,
    ))
    await store.complete(r_old.id)
    # Hand-rewrite to backdate the terminal event so trim sees it as old.
    # (The store writes its own ts_unix=time.time(); to test trim we
    # need to manipulate the event after the fact.)
    path = tmp_path / "c.jsonl"
    text = path.read_text()
    text = text.replace(
        f'"type": "commitment_completed", "ts_unix": ',
        f'"type": "commitment_completed", "ts_unix": ',
    )
    # Easier: rewrite the completed line's at_unix to be 40 days old.
    new_lines = []
    for line in text.splitlines():
        d = json.loads(line)
        if d.get("type") == "commitment_completed" and d.get("id") == r_old.id:
            d["at_unix"] = now - 40 * 86400
        new_lines.append(json.dumps(d))
    path.write_text("\n".join(new_lines) + "\n")

    # Recent completed record (1 day ago) — should keep.
    r_recent = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="recent",
    ))
    await store.complete(r_recent.id)

    dropped = await store.trim(now_unix=now)
    assert dropped == 1
    state = store.current_state()
    assert r_old.id not in state
    assert r_recent.id in state


@pytest.mark.asyncio
async def test_trim_never_drops_pending_no_matter_how_old(tmp_path: Path):
    """A 60-day pending commitment survives every trim."""
    store = CommitmentsStore(
        path=tmp_path / "c.jsonl",
        terminal_retention_days=30,
    )
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="long-deadline",
        created_at_unix=now - 60 * 86400,
        due_window_start_unix=now + 30 * 86400,
    ))
    dropped = await store.trim(now_unix=now)
    assert dropped == 0
    assert rec.id in store.current_state()


@pytest.mark.asyncio
async def test_trim_never_drops_snoozed_indefinitely(tmp_path: Path):
    """Snoozed-far-into-future is still active; trim must leave it."""
    store = CommitmentsStore(
        path=tmp_path / "c.jsonl",
        terminal_retention_days=30,
    )
    now = time.time()
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="snoozed-out",
    ))
    await store.snooze(rec.id, until_unix=now + 365 * 86400)
    dropped = await store.trim(now_unix=now)
    assert dropped == 0
    assert rec.id in store.current_state()


@pytest.mark.asyncio
async def test_trim_empty_file_returns_zero(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    assert await store.trim() == 0


# ─── Path / file behavior ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_file_yields_empty_state(tmp_path: Path):
    """Replay on a never-written file is fine — returns empty dict."""
    store = CommitmentsStore(path=tmp_path / "nonexistent.jsonl")
    assert store.current_state() == {}
    assert store.list() == []


@pytest.mark.asyncio
async def test_add_creates_parent_directory(tmp_path: Path):
    """Store path can include intermediate dirs that don't exist yet."""
    store = CommitmentsStore(path=tmp_path / ".mimir" / "subdir" / "c.jsonl")
    rec = await store.add(CommitmentRecord(
        id=make_commitment_id(), channel_id="c1", text="X",
    ))
    assert store.path.is_file()
    assert rec.id in store.current_state()


@pytest.mark.asyncio
async def test_jsonl_format_is_one_event_per_line(tmp_path: Path):
    store = CommitmentsStore(path=tmp_path / "c.jsonl")
    r1 = await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="c1", text="X1"))
    await store.deliver(r1.id)
    await store.complete(r1.id)

    lines = store.path.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line is valid JSON
