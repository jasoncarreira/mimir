"""Tests for ``mimir.git_tracking`` (PR 4a of MIMIR_HOME_GIT_TRACKING).

Covers the post-turn commit + debounced push contract:
- Empty-porcelain fast path: no commit, no push scheduled.
- Disabled flag: full no-op.
- Un-init'd home (no .git): silent skip.
- Commits when changes present and schedules a debounced push.
- Push failures swallowed → ``git_push_failed`` event emitted.
- Debounce coalescing: 5 commits within the window produce 5 commits
  and exactly 1 push.
- Debounce reset: a new commit cancels the prior pending push.
- ``commit_turn_changes`` swallows commit-stage errors → emits
  ``git_commit_failed`` and skips push scheduling.
- ``health.git_status_summary`` returns (count, top_paths) with
  truncation suffix.

Tests use real ``git`` against ``tmp_path`` repos so the
subprocess-wrapper code path is exercised. A monkeypatched
``DEBOUNCE_SECONDS`` keeps wall-clock dependence small (the spec uses
60s; tests dial it to ~50ms).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mimir import git_tracking, health
from mimir.event_logger import init_logger


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path) -> None:
    """events.jsonl is a module-level singleton; init it per-test."""
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-git")


@pytest.fixture(autouse=True)
def _reset_module() -> None:
    """git_tracking has module-level debounce coordination — reset."""
    git_tracking.reset_module_state()
    yield
    git_tracking.reset_module_state()


@pytest.fixture
def home_repo(tmp_path: Path) -> Path:
    """A bare-bones git repo standing in for /mimir-home. Configured
    so commits land cleanly in CI (no signing, identity in env)."""
    home = tmp_path / "mimir-home"
    home.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=home, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=home, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=home, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=home, check=True,
    )
    # Land an initial commit so HEAD exists.
    (home / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=home, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=home, check=True,
    )
    return home


def _events_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "events.jsonl"


def _read_events(tmp_path: Path) -> list[dict[str, Any]]:
    log = _events_log(tmp_path)
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text().splitlines()
        if line.strip()
    ]


def _short_debounce(
    monkeypatch: pytest.MonkeyPatch, seconds: float = 0.05,
) -> None:
    """Compress the 60s spec window to something test-friendly."""
    monkeypatch.setattr(git_tracking, "DEBOUNCE_SECONDS", seconds)


# ─── disabled-flag and missing-repo paths ───────────────────────────


@pytest.mark.asyncio
async def test_disabled_flag_is_full_noop(home_repo: Path, tmp_path: Path) -> None:
    # Make a tracked-eligible change; we should still NOT see a commit.
    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "x.md").write_text("dirty\n")
    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=False,
    )
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip().count("\n") == 0  # only the seed commit
    assert _read_events(tmp_path) == []


@pytest.mark.asyncio
async def test_uninit_home_silent_skip(tmp_path: Path) -> None:
    # No .git directory — PR 4a may run before mimir setup landed.
    home = tmp_path / "fresh-home"
    home.mkdir()
    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home, enabled=True,
    )
    # Silent: no events, no .git created.
    assert _read_events(tmp_path) == []
    assert not (home / ".git").exists()


# ─── empty-porcelain fast path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_porcelain_no_commit_no_push(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_debounce(monkeypatch)
    pre = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=True,
    )
    post = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert pre == post  # no new commit
    # And critically: no push was scheduled (tracked via module state).
    assert git_tracking._pending_push_task is None
    # Wait past debounce just to make sure nothing fires asynchronously.
    await asyncio.sleep(0.1)
    assert _read_events(tmp_path) == []


# ─── commit + push happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_and_schedule_push(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_debounce(monkeypatch, 0.05)
    # PR 4b: ``_debounced_push`` skips silently when no remote is
    # configured. We want to see the push *attempt* fail here, so add
    # a remote pointing to a nonexistent path — push will then attempt
    # the dial and surface a GitError → git_push_failed event.
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "nonexistent.git")],
        cwd=home_repo, check=True,
    )
    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "x.md").write_text("hello\n")

    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=True,
    )

    # Commit landed.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert log.count("\n") == 2  # seed + new commit
    assert "turn t1 (user_message)" in subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout

    # Wait for the debounced push to fire and fail (no remote set).
    assert git_tracking._pending_push_task is not None
    await asyncio.wait_for(git_tracking._pending_push_task, timeout=2.0)
    events = _read_events(tmp_path)
    push_failures = [e for e in events if e["type"] == "git_push_failed"]
    assert len(push_failures) == 1
    assert push_failures[0]["turn_id"] == "t1"


# ─── debounce coalescing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debounce_coalesces_burst_to_single_push(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 commits within the debounce window produce 5 commits and
    exactly 1 push (the prior 4 push tasks are cancelled before
    firing)."""
    _short_debounce(monkeypatch, 0.10)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "nonexistent.git")],
        cwd=home_repo, check=True,
    )

    (home_repo / "memory").mkdir()
    push_calls = []
    real_git = git_tracking._git

    async def counting_git(*args: str, **kwargs: Any) -> Any:
        if args and args[0] == "push":
            push_calls.append(args)
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(git_tracking, "_git", counting_git)

    for i in range(5):
        (home_repo / "memory" / f"file{i}.md").write_text(f"v{i}\n")
        await git_tracking.commit_turn_changes(
            turn_id=f"t{i}", trigger="user_message", home=home_repo, enabled=True,
        )
        # Stay well under the debounce window so the prior push is
        # cancelled by the next commit.
        await asyncio.sleep(0.01)

    # 5 commits landed on the branch.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert log.count("\n") == 6  # seed + 5

    # Let the debounce expire and the final push fire.
    assert git_tracking._pending_push_task is not None
    try:
        await asyncio.wait_for(git_tracking._pending_push_task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    # Exactly one push attempt — the earlier 4 were cancelled before sleep
    # completed, so they never reached the `git push` invocation.
    assert len(push_calls) == 1, (
        f"expected 1 coalesced push, got {len(push_calls)}: {push_calls}"
    )


@pytest.mark.asyncio
async def test_debounce_reset_cancels_prior_task(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second commit before the debounce expires must cancel the
    prior pending push task and schedule a new one."""
    _short_debounce(monkeypatch, 5.0)  # generous so the task stays pending

    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "a.md").write_text("a\n")
    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=True,
    )
    first_task = git_tracking._pending_push_task
    assert first_task is not None
    assert not first_task.done()

    (home_repo / "memory" / "b.md").write_text("b\n")
    await git_tracking.commit_turn_changes(
        turn_id="t2", trigger="user_message", home=home_repo, enabled=True,
    )
    second_task = git_tracking._pending_push_task
    assert second_task is not None
    assert second_task is not first_task
    # Yield once so the cancellation settles. The task may either land
    # in cancelled() state OR exit cleanly via the
    # "except CancelledError: return" branch in _debounced_push —
    # both are acceptable; what matters is "no push fired" (asserted
    # via the no-events check below).
    try:
        await first_task
    except asyncio.CancelledError:
        pass
    assert first_task.done()

    # No push event should have fired during the debounce window.
    events = _read_events(tmp_path)
    assert [e for e in events if e["type"] == "git_push_failed"] == []

    # Cancel the new one to keep the test fast.
    second_task.cancel()
    try:
        await second_task
    except asyncio.CancelledError:
        pass


# ─── error paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_failure_emits_event_skips_push(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``git commit`` fails (e.g. pre-commit hook refused), we
    log ``git_commit_failed`` and do NOT schedule a push."""
    _short_debounce(monkeypatch, 0.05)

    # Install a refusing pre-commit hook.
    hooks_dir = home_repo / ".git" / "hooks"
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'refused' >&2\nexit 1\n")
    hook.chmod(0o755)

    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "secret.md").write_text("trip the hook\n")
    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=True,
    )

    events = _read_events(tmp_path)
    commit_failures = [e for e in events if e["type"] == "git_commit_failed"]
    assert len(commit_failures) == 1
    assert commit_failures[0]["stage"] == "commit"
    assert commit_failures[0]["turn_id"] == "t1"

    # No push should have been scheduled.
    assert git_tracking._pending_push_task is None
    push_failures = [e for e in events if e["type"] == "git_push_failed"]
    assert push_failures == []


@pytest.mark.asyncio
async def test_push_timeout_logs_git_push_failed(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub ``_git`` so the push branch raises ``asyncio.TimeoutError``
    — verify we surface ``git_push_failed`` with reason='timeout'."""
    _short_debounce(monkeypatch, 0.02)

    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "nonexistent.git")],
        cwd=home_repo, check=True,
    )
    real_git = git_tracking._git

    async def flaky_git(*args: str, **kwargs: Any) -> Any:
        if args and args[0] == "push":
            raise asyncio.TimeoutError()
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(git_tracking, "_git", flaky_git)

    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "z.md").write_text("z\n")
    await git_tracking.commit_turn_changes(
        turn_id="t-timeout",
        trigger="user_message",
        home=home_repo,
        enabled=True,
    )
    assert git_tracking._pending_push_task is not None
    await asyncio.wait_for(git_tracking._pending_push_task, timeout=2.0)

    events = _read_events(tmp_path)
    push_failures = [e for e in events if e["type"] == "git_push_failed"]
    assert len(push_failures) == 1
    assert push_failures[0]["reason"] == "timeout"
    assert push_failures[0]["turn_id"] == "t-timeout"


@pytest.mark.asyncio
async def test_status_failure_emits_git_commit_failed(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the initial status probe blows up, we surface
    ``git_commit_failed`` at stage=status and short-circuit."""

    async def boom(*args: str, **kwargs: Any) -> Any:
        raise OSError("git binary missing")

    monkeypatch.setattr(git_tracking, "_git", boom)

    await git_tracking.commit_turn_changes(
        turn_id="t-status",
        trigger="user_message",
        home=home_repo,
        enabled=True,
    )

    events = _read_events(tmp_path)
    failures = [e for e in events if e["type"] == "git_commit_failed"]
    assert len(failures) == 1
    assert failures[0]["stage"] == "status"
    assert "git binary missing" in failures[0]["error"]


# ─── PR 4b: no-remote skips push silently ───────────────────────────


@pytest.mark.asyncio
async def test_no_remote_skips_push_silently(
    home_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 4b: when no ``origin`` remote is configured, the debounced
    push must skip silently — no ``git push`` invocation, no
    ``git_push_failed`` event spam every turn for what is actually a
    deliberate "init-only, no push target" configuration."""
    _short_debounce(monkeypatch, 0.05)
    push_calls = []
    real_git = git_tracking._git

    async def counting_git(*args: str, **kwargs: Any) -> Any:
        if args and args[0] == "push":
            push_calls.append(args)
        return await real_git(*args, **kwargs)

    monkeypatch.setattr(git_tracking, "_git", counting_git)

    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "x.md").write_text("hello\n")
    await git_tracking.commit_turn_changes(
        turn_id="t1", trigger="user_message", home=home_repo, enabled=True,
    )

    # The commit landed even without a remote.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=home_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert log.count("\n") == 2

    # Wait past debounce.
    assert git_tracking._pending_push_task is not None
    await asyncio.wait_for(git_tracking._pending_push_task, timeout=2.0)

    # Push was NOT invoked, and no failure event surfaced.
    assert push_calls == []
    events = _read_events(tmp_path)
    push_failures = [e for e in events if e["type"] == "git_push_failed"]
    assert push_failures == []


# ─── porcelain summary helper ───────────────────────────────────────


def test_porcelain_summary_truncates() -> None:
    porcelain = (
        " M memory/a.md\n"
        " M memory/b.md\n"
        " M memory/c.md\n"
        " M memory/d.md\n"
        " M memory/e.md\n"
        " M memory/f.md\n"
        " M memory/g.md\n"
    )
    summary = git_tracking._porcelain_summary(porcelain, max_paths=3)
    assert summary.startswith("7 file(s):")
    assert "memory/a.md" in summary
    assert "memory/c.md" in summary
    assert "…+4" in summary
    # The truncated paths should not appear.
    assert "memory/g.md" not in summary


def test_porcelain_summary_handles_rename() -> None:
    porcelain = "R  old/path.md -> new/path.md\n"
    summary = git_tracking._porcelain_summary(porcelain)
    assert "new/path.md" in summary
    assert "old/path.md" not in summary


# ─── health.git_status_summary ──────────────────────────────────────


def test_git_status_summary_uninit_returns_zero(tmp_path: Path) -> None:
    home = tmp_path / "no-git"
    home.mkdir()
    assert health.git_status_summary(home) == (0, [])


def test_git_status_summary_clean_repo(home_repo: Path) -> None:
    # No uncommitted changes — count 0, paths empty.
    assert health.git_status_summary(home_repo) == (0, [])


def test_git_status_summary_dirty_truncates(home_repo: Path) -> None:
    (home_repo / "memory").mkdir()
    for c in "abcdef":
        (home_repo / "memory" / f"{c}.md").write_text(f"{c}\n")
    count, paths = health.git_status_summary(home_repo, top_n=3)
    assert count == 6
    # First 3 paths in lex order, then the truncation marker.
    assert len(paths) == 4
    assert paths[-1] == "…+3"
    assert paths[:3] == sorted(paths[:3])


def test_git_status_summary_dirty_under_topn(home_repo: Path) -> None:
    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "a.md").write_text("a\n")
    (home_repo / "memory" / "b.md").write_text("b\n")
    count, paths = health.git_status_summary(home_repo, top_n=3)
    assert count == 2
    assert paths == ["memory/a.md", "memory/b.md"]


# ─── PR 4b: render_git_status_line ──────────────────────────────────


def test_render_git_status_line_clean_returns_none(home_repo: Path) -> None:
    assert health.render_git_status_line(home_repo) is None


def test_render_git_status_line_uninit_returns_none(tmp_path: Path) -> None:
    home = tmp_path / "no-git"
    home.mkdir()
    assert health.render_git_status_line(home) is None


def test_render_git_status_line_singular_file(home_repo: Path) -> None:
    (home_repo / "memory").mkdir()
    (home_repo / "memory" / "x.md").write_text("x\n")
    line = health.render_git_status_line(home_repo)
    assert line is not None
    # Singular noun, count 1, the path, and the home prefix all present.
    assert ": 1 file —" in line
    assert "memory/x.md" in line
    assert str(home_repo) in line


def test_render_git_status_line_plural_with_truncation(home_repo: Path) -> None:
    (home_repo / "memory").mkdir()
    for c in "abcdef":
        (home_repo / "memory" / f"{c}.md").write_text(f"{c}\n")
    line = health.render_git_status_line(home_repo, top_n=3)
    assert line is not None
    assert ": 6 files —" in line
    assert "memory/a.md" in line
    assert "memory/c.md" in line
    assert "…+3" in line
    # Truncated paths should NOT appear in the rendered text.
    assert "memory/f.md" not in line
