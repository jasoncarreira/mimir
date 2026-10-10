"""Scratch retention janitor — sweep semantics + env knobs + scheduler job."""

from __future__ import annotations

import os
import time
import asyncio
from types import SimpleNamespace
from threading import Event, Thread
from pathlib import Path

import pytest

from mimir.scratch_janitor import (
    DEFAULT_SCRATCH_ROOTS,
    DEFAULT_SCRATCH_TTL_DAYS,
    SweepResult,
    resolve_scratch_roots,
    resolve_scratch_ttl_days,
    sweep_scratch_roots,
)
from mimir.scheduler import Scheduler
from mimir.access_control import ensure_turn_scratch


def _age(path: Path, days: float, *, now: float) -> None:
    """Set ``path``'s (l)mtime to ``days`` before ``now``."""
    ts = now - days * 86400
    os.utime(path, (ts, ts), follow_symlinks=False)


def _make_tree(root: Path, name: str, *, days: float, now: float) -> Path:
    """A dir with a nested file, whole tree aged ``days``."""
    d = root / name
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "payload.bin"
    f.write_bytes(b"x" * 1024)
    for p in (f, d / "sub", d):
        _age(p, days, now=now)
    return d


# ---- sweep_scratch_roots ------------------------------------------------


def test_old_dir_removed_fresh_dir_kept(tmp_path: Path):
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    old = _make_tree(scratch, "pr123-review", days=10, now=now)
    fresh = _make_tree(scratch, "pr999-review", days=1, now=now)

    result = sweep_scratch_roots(tmp_path, ttl_days=7, now=now)

    assert not old.exists()
    assert fresh.exists()
    assert result.removed == ("scratch/pr123-review",)
    assert result.kept == 1
    assert result.bytes_reclaimed >= 1024
    assert result.errors == ()


def test_old_turn_dirs_swept_independently_of_fresh_siblings(tmp_path: Path):
    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    old = [_make_tree(turns, f"old-{i}", days=5, now=now) for i in range(3)]
    fresh = ensure_turn_scratch(tmp_path, "fresh-turn")
    assert fresh is not None
    result = sweep_scratch_roots(tmp_path, now=now)
    assert set(result.removed) == {f"scratch/turns/old-{i}" for i in range(3)}
    assert all(not path.exists() for path in old)
    assert turns.is_dir() and fresh.is_dir()
    assert result.kept == 1
    assert result.errors == ()


def test_turn_dir_with_fresh_nested_file_survives(tmp_path: Path):
    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    old = _make_tree(turns, "old", days=5, now=now)
    used = _make_tree(turns, "used", days=5, now=now)
    (used / "sub" / "payload.bin").touch()
    result = sweep_scratch_roots(tmp_path, now=now)
    assert result.removed == ("scratch/turns/old",)
    assert not old.exists() and used.exists()


@pytest.mark.parametrize("roots", [("scratch",), ("scratch/turns",)])
async def test_active_turn_dir_survives_ttl_in_worker(tmp_path, roots):
    from mimir._context import set_current_turn, reset_current_turn

    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    active = _make_tree(turns, "active", days=5, now=now)
    old = _make_tree(turns, "old", days=5, now=now)
    token = set_current_turn(SimpleNamespace(turn_id="active", turn_scratch_path=active))
    try:
        result = await asyncio.to_thread(sweep_scratch_roots, tmp_path, roots=roots, now=now)
        assert result.protected == ("scratch/turns/active",)
        assert active.is_dir() and not old.exists()
    finally:
        reset_current_turn(token)
    result = sweep_scratch_roots(tmp_path, roots=roots, now=now)
    assert result.removed == ("scratch/turns/active",)
    assert turns.is_dir()


async def test_turn_admitted_after_age_check_is_protected(tmp_path, monkeypatch):
    from mimir import scratch_janitor
    from mimir._context import set_current_turn, reset_current_turn

    now = time.time()
    turn = _make_tree(tmp_path / "scratch" / "turns", "admitted", days=5, now=now)
    inspected = Event()
    proceed = Event()
    original = scratch_janitor._tree_newest_mtime_and_size

    def pause_after_inspection(path, cutoff):
        result = original(path, cutoff)
        if path == turn:
            inspected.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(scratch_janitor, "_tree_newest_mtime_and_size", pause_after_inspection)
    sweep = asyncio.create_task(asyncio.to_thread(sweep_scratch_roots, tmp_path, now=now))
    token = None
    try:
        assert await asyncio.to_thread(inspected.wait, 5)
        token = set_current_turn(SimpleNamespace(turn_id="admitted", turn_scratch_path=turn))
        proceed.set()
        result = await sweep
        assert result.protected == ("scratch/turns/admitted",)
        assert turn.is_dir()
    finally:
        proceed.set()
        await sweep
        if token is not None:
            reset_current_turn(token)


def test_turn_symlink_unlinked_without_sweeping_target(tmp_path):
    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    turns.mkdir(parents=True)
    target = _make_tree(tmp_path, "outside", days=5, now=now)
    link = turns / "old-link"
    link.symlink_to(target)
    _age(link, 5, now=now)
    result = sweep_scratch_roots(tmp_path, now=now)
    assert result.removed == ("scratch/turns/old-link",)
    assert not link.is_symlink() and target.exists()


def test_symlink_turns_container_never_traversed(tmp_path):
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = _make_tree(tmp_path, "outside", days=5, now=now)
    link = scratch / "turns"
    link.symlink_to(target)
    _age(link, 5, now=now)
    result = sweep_scratch_roots(tmp_path, now=now)
    assert result.removed == ("scratch/turns",)
    assert target.exists()


@pytest.mark.parametrize(
    ("root_name", "link_name"),
    [
        ("scratch/turns", "scratch/turns"),
        ("scratch/turns/nested", "scratch/turns"),
        ("scratch/turns/linked-turn", "scratch/turns/linked-turn"),
    ],
)
def test_custom_turns_root_symlink_is_refused(tmp_path, root_name, link_name):
    now = time.time()
    target = tmp_path / "outside"
    nested = _make_tree(target, "nested", days=5, now=now)
    payload = nested / "sub" / "payload.bin"
    expected_payload = payload.read_bytes()
    expected_identity = (payload.stat().st_dev, payload.stat().st_ino)
    _age(target, 5, now=now)
    link = tmp_path / link_name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    _age(link, 5, now=now)

    result = sweep_scratch_roots(tmp_path, roots=(root_name,), now=now)

    assert result.errors == (f"{root_name}: unsafe scratch container",)
    assert result.removed == ()
    assert result.bytes_reclaimed == 0
    assert link.is_symlink() and link.readlink() == target
    assert sorted(target.iterdir()) == [nested]
    assert sorted(nested.iterdir()) == [nested / "sub"]
    assert sorted((nested / "sub").iterdir()) == [payload]
    assert payload.read_bytes() == expected_payload
    assert (payload.stat().st_dev, payload.stat().st_ino) == expected_identity


def test_turn_admission_and_reset_do_not_wait_for_recursive_deletion(tmp_path, monkeypatch):
    from mimir import scratch_janitor
    from mimir._context import set_current_turn, reset_current_turn

    now = time.time()
    turn = _make_tree(tmp_path / "scratch" / "turns", "reused", days=5, now=now)
    deleting = Event()
    lifecycle_done = Event()
    release_delete = Event()
    original = scratch_janitor.rmtree_missing_ok
    failures = []
    results = []

    def paused_delete(path, **kwargs):
        assert not turn.exists()  # Detached before deletion begins.
        assert str(path).startswith(".janitor-trash-")
        assert "dir_fd" in kwargs
        deleting.set()
        assert release_delete.wait(5)
        original(path, **kwargs)

    def sweep():
        try:
            results.append(sweep_scratch_roots(tmp_path, now=now))
        except BaseException as exc:
            failures.append(exc)

    def admit():
        token = set_current_turn(SimpleNamespace(turn_id="reused", turn_scratch_path=turn))
        try:
            assert ensure_turn_scratch(tmp_path, "reused") == turn
            (turn / "new.txt").write_text("new workspace")
        finally:
            reset_current_turn(token)
        lifecycle_done.set()

    monkeypatch.setattr(scratch_janitor, "rmtree_missing_ok", paused_delete)
    worker = Thread(target=sweep)
    admission = Thread(target=admit)
    worker.start()
    try:
        assert deleting.wait(5)
        admission.start()
        # Both admission and teardown must finish WHILE deletion is paused.
        assert lifecycle_done.wait(2)
        assert not release_delete.is_set()
    finally:
        release_delete.set()
        worker.join(5)
        if admission.ident is not None:
            admission.join(5)
    assert not worker.is_alive() and not admission.is_alive()
    assert failures == []
    assert results[0].errors == ()
    assert results[0].removed == ("scratch/turns/reused",)
    assert (turn / "new.txt").read_text() == "new workspace"


@pytest.mark.parametrize("roots", [("scratch",), ("scratch/turns",)])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_turn_container_replaced_after_age_check_is_refused(tmp_path, monkeypatch, roots, replacement):
    from mimir import scratch_janitor

    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    old = _make_tree(turns, "old", days=5, now=now)
    outside = tmp_path / "outside"
    victim = _make_tree(outside, "old", days=5, now=now)
    original = scratch_janitor._tree_newest_mtime_and_size

    def swap_after_inspection(path, cutoff):
        result = original(path, cutoff)
        if path == old:
            turns.rename(turns.with_name("original-turns"))
            if replacement == "symlink":
                turns.symlink_to(outside)
            else:
                turns.mkdir()
                (turns / "old").mkdir()
        return result

    monkeypatch.setattr(scratch_janitor, "_tree_newest_mtime_and_size", swap_after_inspection)
    result = sweep_scratch_roots(tmp_path, roots=roots, now=now)
    assert result.removed == ()
    assert result.errors
    assert victim.is_dir()
    assert (turns.with_name("original-turns") / "old").is_dir()
    assert (turns / "old").is_dir()


def test_turn_container_swapped_at_rename_cannot_redirect_deletion(tmp_path, monkeypatch):
    from mimir import scratch_janitor

    now = time.time()
    turns = tmp_path / "scratch" / "turns"
    _make_tree(turns, "old", days=5, now=now)
    victim = _make_tree(tmp_path / "outside", "old", days=5, now=now)
    original = os.rename
    swaps = []

    def swap_at_rename(src, dst, **kwargs):
        if Path(src).name == "old":
            original(turns, turns.with_name("original-turns"))
            turns.symlink_to(victim.parent)
            swaps.append(True)
        return original(src, dst, **kwargs)

    monkeypatch.setattr(scratch_janitor.os, "rename", swap_at_rename)
    result = sweep_scratch_roots(tmp_path, now=now)
    assert swaps == [True]
    assert result.errors == ()
    assert result.removed == ("scratch/turns/old",)
    assert victim.is_dir()
    assert not (turns.with_name("original-turns") / "old").exists()


def test_failed_trash_deletion_is_retried_on_later_sweep(tmp_path, monkeypatch):
    from mimir import scratch_janitor

    now = time.time()
    turn = _make_tree(tmp_path / "scratch" / "turns", "old", days=5, now=now)
    original = scratch_janitor.rmtree_missing_ok

    def refuse_delete(path, **kwargs):
        raise PermissionError("deletion interrupted")

    monkeypatch.setattr(scratch_janitor, "rmtree_missing_ok", refuse_delete)
    first = sweep_scratch_roots(tmp_path, now=now)
    assert first.errors and not turn.exists()
    trash = list((tmp_path / "scratch").glob(".janitor-trash-*"))
    assert len(trash) == 1
    monkeypatch.setattr(scratch_janitor, "rmtree_missing_ok", original)
    second = sweep_scratch_roots(tmp_path, now=now + 2 * 86400)
    assert second.removed == (str(trash[0].relative_to(tmp_path)),)
    assert second.errors == ()
    assert not trash[0].exists()


def test_nested_fresh_file_keeps_stale_looking_dir(tmp_path: Path):
    """A weeks-old clone the agent touched yesterday must survive."""
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    d = _make_tree(scratch, "long-lived-checkout", days=40, now=now)
    recent = d / "sub" / "notes.md"
    recent.write_text("still in use")
    _age(recent, 0.5, now=now)

    result = sweep_scratch_roots(tmp_path, ttl_days=7, now=now)

    assert d.exists()
    assert result.removed == ()
    assert result.kept == 1


def test_loose_files_swept_by_age(tmp_path: Path):
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    old = scratch / "pr528.diff"
    old.write_bytes(b"y" * 2048)
    _age(old, 30, now=now)
    fresh = scratch / "today.log"
    fresh.write_text("hot")
    _age(fresh, 0.1, now=now)

    result = sweep_scratch_roots(tmp_path, ttl_days=7, now=now)

    assert not old.exists()
    assert fresh.exists()
    assert result.removed == ("scratch/pr528.diff",)
    assert result.bytes_reclaimed >= 2048


def test_symlink_entry_unlinked_target_untouched(tmp_path: Path):
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("do not delete")
    link = scratch / "stale-link"
    link.symlink_to(target)
    _age(link, 30, now=now)

    result = sweep_scratch_roots(tmp_path, ttl_days=7, now=now)

    assert not link.exists()
    assert (target / "keep.txt").read_text() == "do not delete"
    assert result.removed == ("scratch/stale-link",)


def test_missing_root_is_silent_noop(tmp_path: Path):
    result = sweep_scratch_roots(tmp_path, ttl_days=7)
    assert result == SweepResult()


async def test_fetch_cache_retained_through_turn_lifecycle(tmp_path):
    from mimir._context import set_current_turn, reset_current_turn

    now = time.time()
    cache = tmp_path / "attachments" / "fetch-cache"
    cache.mkdir(parents=True)
    files = [cache / name for name in ("body.pdf", "body.pdf.meta.json", "body.pdf.txt")]
    for path in files:
        path.write_text("cached content")
        _age(path, 10, now=now)
    inbound = tmp_path / "attachments" / "inbound"
    inbound.mkdir()
    attachment = inbound / "keep.txt"
    attachment.write_text("not cache")
    _age(attachment, 10, now=now)
    token = set_current_turn(SimpleNamespace(turn_id="janitor-cache-test"))
    try:
        result = await asyncio.to_thread(sweep_scratch_roots, tmp_path, now=now)
        assert set(result.protected) == {str(p.relative_to(tmp_path)) for p in files}
        # The turn reads only after the janitor worker has completed.
        assert all(p.read_text() == "cached content" for p in files)
    finally:
        reset_current_turn(token)
    result = await asyncio.to_thread(sweep_scratch_roots, tmp_path, now=now)
    assert set(result.removed) == {str(p.relative_to(tmp_path)) for p in files}
    assert attachment.exists()


async def test_turn_starting_during_sweep_prevents_cache_unlink(tmp_path, monkeypatch):
    from mimir import scratch_janitor
    from mimir._context import set_current_turn, reset_current_turn

    now = time.time()
    cache = tmp_path / "attachments" / "fetch-cache"
    cache.mkdir(parents=True)
    body = cache / "body.html"
    body.write_text("still readable")
    _age(body, 10, now=now)
    inspected = Event()
    proceed = Event()
    original = scratch_janitor._tree_newest_mtime_and_size

    def pause_after_inspection(path, cutoff):
        result = original(path, cutoff)
        if path == body:
            inspected.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(scratch_janitor, "_tree_newest_mtime_and_size", pause_after_inspection)
    sweep = asyncio.create_task(asyncio.to_thread(sweep_scratch_roots, tmp_path, now=now))
    token = None
    try:
        assert await asyncio.to_thread(inspected.wait, 5)
        token = set_current_turn(SimpleNamespace(turn_id="janitor-admission-race"))
        proceed.set()
        result = await sweep
        assert result.protected == ("attachments/fetch-cache/body.html",)
        assert body.read_text() == "still readable"
    finally:
        proceed.set()
        await sweep
        if token is not None:
            reset_current_turn(token)


def test_escaping_roots_rejected(tmp_path: Path):
    now = time.time()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    victim = _make_tree(outside, "victim", days=30, now=now)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # Symlinked root escaping the home is rejected on resolved containment.
    (tmp_path / "evil").symlink_to(outside)

    result = sweep_scratch_roots(
        tmp_path,
        ttl_days=7,
        roots=("..", "/etc", "evil", "a/../..", ""),
        now=now,
    )

    assert victim.exists()
    assert result.removed == ()


def test_nested_relative_root_swept(tmp_path: Path):
    now = time.time()
    transcripts = tmp_path / "state" / "worklink" / "transcripts"
    transcripts.mkdir(parents=True)
    old = transcripts / "opencode-905-20260601T000000Z.json"
    old.write_text("{}")
    _age(old, 30, now=now)

    result = sweep_scratch_roots(
        tmp_path, ttl_days=7, roots=("state/worklink/transcripts",), now=now
    )

    assert not old.exists()
    assert result.removed == (
        "state/worklink/transcripts/opencode-905-20260601T000000Z.json",
    )


def test_nonpositive_ttl_is_noop(tmp_path: Path):
    now = time.time()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    old = _make_tree(scratch, "old", days=100, now=now)

    for ttl in (0, -3):
        result = sweep_scratch_roots(tmp_path, ttl_days=ttl, now=now)
        assert old.exists()
        assert result.removed == ()


# ---- env knobs -----------------------------------------------------------


def test_resolve_ttl_days():
    assert resolve_scratch_ttl_days("") == DEFAULT_SCRATCH_TTL_DAYS
    assert resolve_scratch_ttl_days("14") == 14
    assert resolve_scratch_ttl_days("0") == 0
    assert resolve_scratch_ttl_days("-1") == -1
    assert resolve_scratch_ttl_days("banana") == DEFAULT_SCRATCH_TTL_DAYS


def test_resolve_ttl_days_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_SCRATCH_TTL_DAYS", "3")
    assert resolve_scratch_ttl_days() == 3


def test_resolve_roots():
    assert resolve_scratch_roots("") == DEFAULT_SCRATCH_ROOTS
    assert resolve_scratch_roots("scratch,.review-scratch") == (
        "scratch",
        ".review-scratch",
    )
    # Nested relative allowed; absolute / ``..`` / dupes dropped.
    assert resolve_scratch_roots(
        "state/worklink/transcripts, /etc, .., scratch, scratch, a/../b"
    ) == ("state/worklink/transcripts", "scratch")
    # Nothing valid -> fall back to the default, never an empty sweep-all.
    assert resolve_scratch_roots("..,/etc") == DEFAULT_SCRATCH_ROOTS


def test_resolve_roots_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_SCRATCH_JANITOR_ROOTS", "scratch,.review-scratch")
    assert resolve_scratch_roots() == ("scratch", ".review-scratch")


# ---- scheduler job -------------------------------------------------------


async def _noop_enqueue(_e):
    return True


def test_janitor_empty_cron_does_not_install_job(tmp_path: Path):
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=_noop_enqueue)
    assert sched.add_scratch_janitor_job(tmp_path, cron_expr="") is False
    assert sched._scheduler.get_job("scratch-janitor") is None
    assert "scratch-janitor" in sched.registered_callables()


def test_janitor_zero_ttl_skips_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MIMIR_SCRATCH_TTL_DAYS", "0")
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=_noop_enqueue)
    assert sched.add_scratch_janitor_job(tmp_path) is False
    assert sched._scheduler.get_job("scratch-janitor") is None


def test_janitor_default_cron_installs_job(tmp_path: Path):
    sched = Scheduler(scheduler_yaml=tmp_path / "s.yaml", enqueue=_noop_enqueue)
    assert sched.add_scratch_janitor_job(tmp_path) is True
    assert sched._scheduler.get_job("scratch-janitor") is not None
