"""Scratch retention janitor — sweep semantics + env knobs + scheduler job."""

from __future__ import annotations

import os
import time
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
