"""Post-turn git commit + debounced push for /mimir-home.

Implements PR 4a of the MIMIR_HOME_GIT_TRACKING.md spec: a small module
the agent calls in the post-message phase of every turn. The common case
(no memory writes this turn) is a single ``git status --porcelain`` call
and an early return — most turns don't touch tracked state.

Design contract (see spec §"Post-turn commit hook contract"):

- **Empty-porcelain turns are free.** No commit, no push, no debounce
  scheduling. Return after the porcelain check (~5ms).
- **Per-turn commit, debounced push.** Each turn that touches tracked
  state gets its own commit (audit-trail granularity) but pushes
  coalesce on a 60s window.
- **Debounce semantics.** Each new commit cancels the prior pending
  push task and reschedules. A burst of N commits within <60s
  produces N commits and 1 push.
- **Push failures never block the next turn.** They surface as
  ``git_push_failed`` algedonic events.
- **Behavior gated on ``MIMIR_GIT_TRACKING_ENABLED=true``.** Default
  off so PR 4a lands inert ahead of the gitignore + secret hook in
  PR 4b. When disabled, ``commit_turn_changes`` is a no-op.

The module is a singleton-by-coordination: ``_pending_push_task`` and
``_push_debounce_lock`` live at module scope so concurrent turns share
the same debounce window. Tests reset module state via
``reset_module_state()``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .event_logger import log_event

log = logging.getLogger(__name__)


# ─── module-level coordination ────────────────────────────────────────

# CR2 (external I/O) fix: keyed by home path, not singleton. Pre-fix
# ``_push_debounce_lock`` and ``_pending_push_task`` were single
# module globals — a turn that committed in one repo path would
# cancel a pending push in a *different* repo path if the agent ever
# served more than one home. Today only one home is served per
# process so this was dormant, but the singleton coupled
# theoretically-independent agents (multi-home dev, integration
# tests running the agent twice) and made the function untestable
# in parallel. Per-home dicts keep state isolated; existing API
# (``commit_turn_changes(home=...)``) flows the discriminator
# through naturally.
_push_debounce_locks: dict[str, asyncio.Lock] = {}
_pending_push_tasks: dict[str, asyncio.Task] = {}
_push_retry_tasks: dict[str, asyncio.Task | None] = {}

# Retry schedule after a push failure. Tests monkeypatch this.
PUSH_RETRY_DELAYS: tuple[float, ...] = (300.0, 900.0, 2700.0)  # 5m, 15m, 45m


def __getattr__(name: str):
    """Backwards-compat shim for the old singleton API. Tests (and any
    operator script that introspects module state) can still read
    ``git_tracking._pending_push_task`` and get the live task for the
    one-home case. Raises AttributeError when more than one home is
    active so multi-home callers see a clear signal that they need
    to use the per-home dict directly."""
    if name == "_pending_push_task":
        active = [t for t in _pending_push_tasks.values() if t is not None]
        if not active:
            return None
        if len(active) == 1:
            return active[0]
        # Multi-home is the case the per-home shape was added to support;
        # the singleton accessor can't disambiguate.
        raise AttributeError(
            f"_pending_push_task is per-home now ({len(active)} active "
            f"tasks); use _pending_push_tasks dict keyed by resolved "
            f"home path."
        )
    if name == "_push_debounce_lock":
        active = list(_push_debounce_locks.values())
        if not active:
            return None
        if len(active) == 1:
            return active[0]
        raise AttributeError(
            f"_push_debounce_lock is per-home now ({len(active)} locks); "
            f"use _push_debounce_locks dict keyed by resolved home path."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Tunables — callers can override for tests. Spec says 60s debounce,
# 30s push timeout, 10s per-call timeout for non-push commands.
DEBOUNCE_SECONDS = 60.0
PUSH_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 10.0


def _home_key(home: Path) -> str:
    """Resolve home to a canonical key for the per-home state dicts.
    Use the resolved absolute path so symlinks / relative paths
    don't produce two keys for the same physical home."""
    try:
        return str(home.resolve())
    except OSError:
        return str(home)


def _get_lock(home: Path) -> asyncio.Lock:
    """Lazy lock creation — asyncio.Lock binds to the running loop, so
    we can't create it at import time (no loop yet)."""
    key = _home_key(home)
    lock = _push_debounce_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _push_debounce_locks[key] = lock
    return lock


def reset_module_state() -> None:
    """Test helper — drop debounce state so each test starts clean."""
    for task in list(_pending_push_tasks.values()):
        if task is not None and not task.done():
            task.cancel()
    _pending_push_tasks.clear()
    for task in list(_push_retry_tasks.values()):
        if task is not None and not task.done():
            task.cancel()
    _push_retry_tasks.clear()
    _push_debounce_locks.clear()


# ─── git error type + subprocess wrapper ─────────────────────────────


class GitError(RuntimeError):
    """A git invocation returned non-zero. ``returncode``/``stderr``
    are exposed for callers that want to discriminate (e.g. "nothing
    to commit" is a soft path)."""

    def __init__(self, returncode: int, stderr: str, cmd: tuple[str, ...]) -> None:
        super().__init__(
            f"git {' '.join(cmd)} failed (rc={returncode}): {stderr.strip()}"
        )
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = cmd


@dataclass
class GitResult:
    stdout: str
    stderr: str


async def _git(
    *args: str,
    cwd: Path,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> GitResult:
    """Run ``git <args>`` under ``cwd`` and return (stdout, stderr).

    Raises ``GitError`` on non-zero exit or ``asyncio.TimeoutError`` on
    timeout. Used for every git invocation in this module so retry/timeout
    behaviour is uniform.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        # Make sure we don't leak a runaway git process on timeout.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise GitError(proc.returncode or -1, stderr, args)
    return GitResult(stdout=stdout, stderr=stderr)


# ─── public API: commit_turn_changes ──────────────────────────────────


def _porcelain_summary(porcelain: str, max_paths: int = 5) -> str:
    """Build a compact "X file(s): a, b, c…+N" summary for the commit
    message body. Each line of porcelain output is "<XY> <path>"."""
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # Porcelain: 2 status chars + space + path. Tolerate rename
        # entries like "R  old -> new" by taking the post-arrow side.
        path = line[3:].strip() if len(line) >= 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    if not paths:
        return ""
    head = paths[: max_paths]
    suffix = f"…+{len(paths) - max_paths}" if len(paths) > max_paths else ""
    return f"{len(paths)} file(s): {', '.join(head)}{suffix}"


async def commit_turn_changes(
    *,
    turn_id: str,
    trigger: str,
    home: Path,
    enabled: bool = True,
) -> None:
    """Commit any human-readable state changes from this turn, then
    schedule a debounced push.

    Called from ``Agent._post_message_hook`` (or the post-message phase
    of ``run_turn``) after the message buffer flushes. Behavior gated
    on ``MIMIR_GIT_TRACKING_ENABLED=true`` — when disabled the function
    is a no-op so PR 4a can land inert.

    The common case (no memory writes this turn) is a single
    ``git status --porcelain`` call returning empty and we return
    early — no commit, no push, ~5ms overhead.
    """
    if not enabled:
        return
    if not (home / ".git").exists():
        # PR 4a lands ahead of ``mimir setup`` (PR 4b), so the volume
        # may not be a git repo yet. Silently skip; the hook becomes
        # active once .git is in place. Not a failure.
        return

    # 1. Fast check — anything to commit? Most turns hit this branch.
    try:
        result = await _git("status", "--porcelain", cwd=home)
    except (GitError, asyncio.TimeoutError, OSError) as exc:
        await log_event(
            "git_commit_failed",
            stage="status",
            turn_id=turn_id,
            error=_short_err(exc),
        )
        return
    if not result.stdout.strip():
        return  # no-op fast path; do NOT schedule a push.

    porcelain = result.stdout

    # 2. Stage everything not gitignored. -A respects .gitignore.
    try:
        await _git("add", "-A", cwd=home)
    except (GitError, asyncio.TimeoutError, OSError) as exc:
        await log_event(
            "git_commit_failed",
            stage="add",
            turn_id=turn_id,
            error=_short_err(exc),
        )
        return

    # 3. Commit. Auto message references turn_id + trigger.
    summary = _porcelain_summary(porcelain)
    msg = f"turn {turn_id} ({trigger})"
    if summary:
        msg = f"{msg}\n\n{summary}"
    try:
        await _git("commit", "-m", msg, cwd=home)
    except GitError as exc:
        # "nothing to commit" can happen if everything got gitignored
        # between status and add. Treat as soft no-op.
        if "nothing to commit" in (exc.stderr or "") or "nothing to commit" in str(exc):
            return
        await log_event(
            "git_commit_failed",
            stage="commit",
            turn_id=turn_id,
            error=_short_err(exc),
        )
        return
    except (asyncio.TimeoutError, OSError) as exc:
        await log_event(
            "git_commit_failed",
            stage="commit",
            turn_id=turn_id,
            error=_short_err(exc),
        )
        return

    # 4. Schedule a debounced push. Subsequent calls within the
    #    debounce window cancel the pending task and reschedule, so
    #    a burst of N commits in <60s becomes 1 push.
    await _schedule_debounced_push(turn_id=turn_id, home=home)


# ─── debounced push coordination ─────────────────────────────────────


async def _schedule_debounced_push(*, turn_id: str, home: Path) -> None:
    """Cancel any pending debounced push for this home and schedule a
    fresh one.

    Holds the per-home lock briefly so two concurrent turns on the
    SAME home can't both create push tasks. CR2 fix: state is keyed
    by home so two concurrent turns on DIFFERENT homes (multi-home
    dev / parallel tests) no longer collide.
    """
    key = _home_key(home)
    # Cancel any pending retry task before creating a new debounce — the new
    # debounce push will cover all unpushed commits, making the retry redundant.
    existing_retry = _push_retry_tasks.pop(key, None)
    if existing_retry is not None and not existing_retry.done():
        existing_retry.cancel()
    async with _get_lock(home):
        existing = _pending_push_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()
        _pending_push_tasks[key] = asyncio.create_task(
            _debounced_push(turn_id=turn_id, home=home)
        )


async def _debounced_push(*, turn_id: str, home: Path) -> None:
    """Sleep ``DEBOUNCE_SECONDS`` then push. If cancelled (a later
    commit superseded us), exit silently — that turn's task owns the
    push instead. On push failure log ``git_push_failed`` and let the
    next successful debounce catch up.

    PR 4b: skip the push silently when no ``origin`` remote is
    configured. The bootstrap path can leave the repo with no remote
    (operator hasn't set ``MIMIR_STATE_REPO`` + ``GITHUB_TOKEN``); we
    still commit-per-turn (audit trail value), but don't churn
    ``git_push_failed`` events on every turn for a missing remote
    that's a configuration choice, not a failure.
    """
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # superseded; the new task owns the push.
    if not await _has_origin_remote(home):
        return
    key = _home_key(home)
    try:
        await _git("push", cwd=home, timeout=PUSH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await log_event(
            "git_push_failed",
            reason="timeout",
            timeout_s=PUSH_TIMEOUT_SECONDS,
            turn_id=turn_id,
        )
        existing = _push_retry_tasks.get(key)
        if existing is None or existing.done():
            _push_retry_tasks[key] = asyncio.create_task(
                _retry_push(home=home, delay=PUSH_RETRY_DELAYS[0], attempt=0, turn_id=turn_id)
            )
        return
    except GitError as exc:
        await log_event(
            "git_push_failed",
            reason=_short_err(exc),
            returncode=exc.returncode,
            turn_id=turn_id,
        )
        existing = _push_retry_tasks.get(key)
        if existing is None or existing.done():
            _push_retry_tasks[key] = asyncio.create_task(
                _retry_push(home=home, delay=PUSH_RETRY_DELAYS[0], attempt=0, turn_id=turn_id)
            )
        return
    except (OSError, asyncio.CancelledError) as exc:
        # OSError: git binary missing / fork failed. CancelledError
        # post-sleep is exotic but treat the same — log and move on.
        if isinstance(exc, asyncio.CancelledError):
            return
        await log_event(
            "git_push_failed",
            reason=_short_err(exc),
            turn_id=turn_id,
        )
        existing = _push_retry_tasks.get(key)
        if existing is None or existing.done():
            _push_retry_tasks[key] = asyncio.create_task(
                _retry_push(home=home, delay=PUSH_RETRY_DELAYS[0], attempt=0, turn_id=turn_id)
            )
        return
    # Success — cancel any pending retry (e.g. from a prior failure in
    # this session) before emitting the ok event.
    existing_retry = _push_retry_tasks.pop(key, None)
    if existing_retry is not None and not existing_retry.done():
        existing_retry.cancel()
    # chainlink #65 (sub B): paired-positive emit. The push succeeded;
    # surface it so the algedonic block can show "old git_push_failed
    # + recent git_push_ok = transient, recovered" against the sticky
    # 24h failure line. First-occurrence-only at the feedback layer
    # keeps the latest success the live state.
    await log_event(
        "git_push_ok",
        turn_id=turn_id,
    )


# ─── helpers ─────────────────────────────────────────────────────────


async def _has_origin_remote(home: Path) -> bool:
    """Return True iff ``git remote get-url origin`` succeeds.

    Used by ``_debounced_push`` to skip pushes when the operator hasn't
    configured a remote — keeps the offline / init-only path quiet
    (no ``git_push_failed`` per turn for an absent-by-design remote).
    """
    try:
        await _git("remote", "get-url", "origin", cwd=home)
        return True
    except (GitError, asyncio.TimeoutError, OSError):
        return False


async def _retry_push(*, home: Path, delay: float, attempt: int, turn_id: str) -> None:
    """Retry push after backoff. Self-chains: on failure, schedules the next retry
    (or emits git_push_stale when retries exhausted). Cancellable: debounce push
    or reset_module_state cancel this task to stop the chain."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # superseded by new commit debounce or shutdown

    if not await _has_origin_remote(home):
        return

    key = _home_key(home)
    # Try the push.
    error_reason = None
    error_returncode = None
    try:
        await _git("push", cwd=home, timeout=PUSH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        error_reason = "timeout"
    except GitError as exc:
        error_reason = _short_err(exc)
        error_returncode = exc.returncode
    except asyncio.CancelledError:
        return
    except OSError as exc:
        error_reason = _short_err(exc)

    if error_reason is None:
        # Success.
        _push_retry_tasks.pop(key, None)
        extra: dict[str, Any] = {"via": "retry", "attempt": attempt + 1}
        await log_event("git_push_ok", turn_id=turn_id, **extra)
        return

    # Failure — log it.
    fail_extra: dict[str, Any] = {"reason": error_reason, "attempt": attempt + 1, "turn_id": turn_id}
    if error_returncode is not None:
        fail_extra["returncode"] = error_returncode
    await log_event("git_push_failed", **fail_extra)

    # Chain to next retry or escalate.
    next_attempt = attempt + 1
    if next_attempt >= len(PUSH_RETRY_DELAYS):
        # Retries exhausted — emit algedonic escalation.
        unpushed = await _count_unpushed_commits(home)
        await log_event("git_push_stale", unpushed_commits=unpushed, attempts=next_attempt, turn_id=turn_id)
        _push_retry_tasks.pop(key, None)
        return

    # Schedule next retry. This overwrites our own ref in the dict (we're done
    # after this return; the new task is the live retry).
    _push_retry_tasks[key] = asyncio.create_task(
        _retry_push(home=home, delay=PUSH_RETRY_DELAYS[next_attempt], attempt=next_attempt, turn_id=turn_id)
    )


async def _count_unpushed_commits(home: Path) -> int:
    """Count commits on HEAD not yet pushed to the upstream.

    First tries the tracking-branch refspec ``@{upstream}..HEAD``. If no
    upstream is configured (fresh branch, never pushed), falls back to
    ``origin/HEAD..HEAD``. If that also fails (remote HEAD not set), falls
    back to counting all local commits on HEAD — a safe over-count that is
    better than returning 0 when we know a push is failing. Returns 0 on
    any other error.
    """
    for refspec in ("@{upstream}..HEAD", "origin/HEAD..HEAD"):
        try:
            result = await _git(
                "rev-list", "--count", refspec,
                cwd=home, timeout=COMMAND_TIMEOUT_SECONDS,
            )
            return int(result.stdout.strip())
        except (GitError, asyncio.TimeoutError, OSError, ValueError):
            continue
    # Final fallback: count all commits on HEAD. Over-counts if some have
    # been pushed (e.g. by a previous session) but is directionally correct.
    try:
        result = await _git(
            "rev-list", "--count", "HEAD",
            cwd=home, timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return int(result.stdout.strip())
    except (GitError, asyncio.TimeoutError, OSError, ValueError):
        return 0


def _short_err(exc: BaseException) -> str:
    """Single-line, safely-truncated error description for events.jsonl.
    Long stderr (e.g. multi-line git error blocks) gets squashed to a
    single line so the algedonic block stays readable."""
    text = str(exc) or exc.__class__.__name__
    return " ".join(text.split())[:500]


__all__: tuple[str, ...] = (
    "GitError",
    "GitResult",
    "commit_turn_changes",
    "reset_module_state",
    "DEBOUNCE_SECONDS",
    "PUSH_TIMEOUT_SECONDS",
    "PUSH_RETRY_DELAYS",
)
