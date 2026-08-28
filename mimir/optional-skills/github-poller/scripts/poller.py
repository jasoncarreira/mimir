#!/usr/bin/env python3
"""GitHub repository poller — pollers.json contract (chainlink #3).

Checks each ``GITHUB_REPOS`` entry for new issues, PRs, conversation
comments, PR review comments (inline diff), PR reviews, and newly completed
check failures on open PRs. Emits one JSONL event per actionable item.

Differences from the open-strix port this is based on:

- Adds the ``check_pr_review_comments`` pass (inline diff comments via
  ``/repos/{repo}/pulls/comments``) — open-strix's poller missed these,
  which are the bulk of code-review feedback for open PRs.
- Replaces ``gh api user`` auto-detect for self-filtering with an
  explicit ``MIMIR_GITHUB_SELF_LOGIN`` env var. The auto-detect was
  wrong when the container's PAT belongs to the operator (Jason's
  case) — filtering Jason out would silence the very signal we want.
  Empty / unset ``MIMIR_GITHUB_SELF_LOGIN`` → no self-filter.
- Cursor lives at ``$STATE_DIR/cursor.json`` which the mimir framework
  resolves to ``<home>/state/pollers/<poller_name>/`` (persistent
  across container rebuilds, separate from the skill dir).

The cursor advances after every successful run regardless of per-repo
or per-resource ``gh api`` failures: a transient rate-limit / 5xx /
network error on one repo's endpoint silently drops events in that
cursor window. The alternative — pinning the cursor on partial
failure — wedges polling indefinitely if one repo is persistently
broken, so this is the deliberate tradeoff. Persistent failures
surface as ``poller_stderr`` events for the affected endpoints, so
operator audit can grep for them.

Exception — review-requests (chainlink #299): that "advance regardless"
tradeoff covers POLL-side (gh-api) failures, NOT the downstream review
TURN failing. A ``pr_review_requested`` whose triggered turn dies (e.g.
a transient model 503) would otherwise vanish — the cursor recorded the
request as "already seen," so it never re-fired and the review was
silently dropped (observed on PR #511). The review-request cursor now
stores a per-PR ATTEMPT COUNT and RE-EMITS while ``me`` remains a
requested reviewer — a submitted review removes ``me`` from
``requested_reviewers``, so "still requested" means "review still
pending" — bounded by ``REVIEW_REQUEST_MAX_ATTEMPTS``. On exhaustion it
emits a one-shot ``pr_review_request_gave_up`` signal (negative
algedonic; ``feedback.classify`` maps the ``*_gave_up`` suffix) and goes
dormant for that PR. The bound is the wedge guard the original tradeoff
was protecting against.

Environment variables:
    STATE_DIR                  - Persistent state dir (set by framework)
    POLLER_NAME                - This poller's name
    GITHUB_REPOS               - Comma-separated owner/repo list (REQUIRED)
    GITHUB_TOKEN               - Optional; falls back to ``gh auth token``
    MIMIR_GITHUB_SELF_LOGIN    - Optional; events from this login are filtered

Output contract:
    stdout: JSONL — {"poller": str, "prompt": str, ...} per event
    stderr: diagnostic logging
    exit 0: success (zero events is fine — silence means nothing new)
    non-zero: error (the framework drops any emitted events for the run)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

#: Wall clock at interpreter start for this tick, captured before any of the
#: module's own setup runs. The framework kills the tick at a fixed cap measured
#: from process start, so a budget anchored at its own construction silently
#: excludes everything that happened first — module import, cursor load, token
#: resolution. Measured at ~0.8s today, but the bound must not depend on that
#: staying small: `/mimir-home` is virtiofs and the package keeps growing.
_PROCESS_START = time.monotonic()
from datetime import datetime, timedelta, timezone
from pathlib import Path

def _ensure_mimir_import_path() -> None:
    """Let an installed optional-skill poller import the source checkout.

    Optional poller commands run as subprocesses from the installed skill dir, so
    ``sys.path[0]`` is the skill directory rather than the mimir source checkout.
    Prefer an explicit ``MIMIR_SOURCE_DIR`` supplied by the runtime, then fall
    back to the common editable-source layout where the interpreter lives in
    ``<source>/.venv/bin``. Package installs do not need this repair because
    ``mimir`` is already in site-packages.

    This duplicates ``chainlink-orchestrator/scripts/poller.py``'s copy deliberately: the
    repair has to run *before* ``mimir`` is importable, so it cannot itself live in
    the ``mimir`` package. Each installed skill is a standalone directory, so a
    shared helper would have to be copied in anyway. The invariant is covered by a
    test that executes every optional-skill poller the way production does, rather
    than by comparing the two copies textually.
    """

    # Keep the lexical venv path: bin/python is commonly a symlink to the base
    # interpreter, and resolving it would erase the venv identity.
    exe = Path(sys.executable)
    venv_root = exe.parent.parent
    # When running directly from a source checkout, keep the script and imported
    # package on that same revision even if MIMIR_SOURCE_DIR points at another
    # checkout. Installed copies fail the __init__.py probe and fall through.
    script_path = globals().get("__file__")
    candidates = [Path(script_path).resolve().parents[4]] if script_path else []
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(source_dir))
    if venv_root.name in {".venv", "venv"}:
        candidates.append(venv_root.parent)
    for candidate in candidates:
        if (candidate / "mimir" / "__init__.py").is_file():
            # Source checkout first, so ``import mimir`` resolves to the checked-out
            # code even when the poller is installed under <home>/skills.
            path = str(candidate)
            # It may already be present later in sys.path (notably when tests run
            # a worktree script beside the primary checkout). Move, don't merely
            # add, so script and imported package always come from one checkout.
            while path in sys.path:
                sys.path.remove(path)
            sys.path.insert(0, path)

            # Production poller commands may run under system ``python3`` rather
            # than the mimir venv interpreter. In editable-source deployments the
            # checked-out repo's venv holds runtime deps such as PyYAML, so add
            # its site-packages too. This is a best-effort repair: pip-installed
            # deployments already have dependencies on sys.path, and missing venvs
            # simply fall through to the normal ImportError if deps are absent.
            venv = candidate / ".venv"
            if venv.is_dir():
                for site in sorted((venv / "lib").glob("python*/site-packages")):
                    site_path = str(site)
                    if site_path not in sys.path:
                        sys.path.append(site_path)
            return


_ensure_mimir_import_path()

# Must stay AFTER the repair above. These resolve author trust server-side and are
# what makes the collaborator-only review filter work; if they cannot be imported
# the poller must fail rather than run without the filter, because "no filter"
# means auto-reviewing every author's PR — the exact behaviour #1022 removed.
from mimir.pollers import _github_author_is_trusted, _github_content_author

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent.parent))
CURSOR_FILE = STATE_DIR / "cursor.json"
POLLER_NAME = os.environ.get("POLLER_NAME", "github-activity")

# First-run lookback window so cursor=0 doesn't backfill the entire
# repo history. 1 hour is generous for 15-min polls without flooding.
FIRST_RUN_LOOKBACK = timedelta(hours=1)

# Truncate body excerpts so a 50-line review comment doesn't blow the
# event prompt budget. The framework also caps prompts at ~16 KB; this
# is the per-field cap before that runs.
BODY_PREVIEW_CHARS = 300

# chainlink #299: max ``pr_review_requested`` emits for the SAME PR while
# ``me`` stays a requested reviewer, before giving up. The re-emit is a
# state-reconciling retry — a submitted review clears ``me`` from
# ``requested_reviewers``, so "still requested" means the review never
# landed (e.g. the triggered turn hit a transient failure). Bounded so a
# persistently-unreviewable PR can't re-fire forever (the wedge guard).
# At ~15-min polls this is ~3 retries over ~45 min before the give-up
# signal fires.
REVIEW_REQUEST_MAX_ATTEMPTS = 3

_RECOVERY_STATE_FILE = ".recovery.json"
_REVIEW_TURN_EVENT_TYPES = frozenset({
    "pr_opened", "pr_synchronize", "pr_review_requested",
})
_CHANGES_REQUESTED_TURN_EVENT_TYPES = frozenset({"pr_changes_requested_stale"})

# Unresolved review feedback is re-reminded on elapsed time rather than poll
# count. A one-minute jitter allowance absorbs small per-run timestamp drift
# without changing the intended hourly cadence. After bounded attempts give up,
# a daily backstop starts a fresh series so an unresolved PR cannot go silent
# forever merely because its queued turns were never delivered.
# The framework SIGKILLs a poller tick at its cap and discards everything the tick
# emitted, so per-PR reconciliation has to fit inside a self-imposed deadline that
# leaves headroom. Reconciling every open PR in every pass exceeded the cap at 16
# open PRs, which wedged this poller: a killed tick never commits its cursor, so
# the next tick re-scans the same window plus everything new and is guaranteed to
# be larger (chainlink #1433). A tick that truncates and commits is strictly
# better than one that completes nothing.
#
# The deadlines below are derived from the cap rather than hardcoded against it.
# They were originally 35s/50s against a 60s cap; the cap is now per-poller
# (mimir/pollers.py raised the default to 120s and clamps it below each poller's
# own cadence), so a hardcoded copy would silently leave the extra headroom
# unused — and would be wrong in the dangerous direction for any poller whose
# cadence clamps the cap *below* 60s.
def _env_float(name: str, default: float) -> float:
    """Positive float from the environment, or ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: The cap this run will actually be killed at. ``run_poller`` exports the
#: *effective* per-poller value, which may be clamped below the framework default
#: when a poller's cadence is tighter. The 60s fallback is deliberately the old
#: cap rather than the new one: a standalone or dry-run invocation with no
#: framework around it should assume less headroom, not more.
POLLER_CAP_SECONDS = _env_float("POLLER_TIMEOUT_SECONDS", 60.0)
#: Reserved after the hard deadline for saving the cursor and flushing stdout.
#: Overrunning the cap loses every event the tick emitted, so this margin is what
#: separates a late tick from a lost one.
TICK_SAVE_RESERVE_SECONDS = 10.0
#: Hard stop. Past this point no new per-PR work starts and every outstanding API
#: call is clamped to whatever time is left.
TICK_HARD_DEADLINE_SECONDS = max(5.0, POLLER_CAP_SECONDS - TICK_SAVE_RESERVE_SECONDS)
#: Fraction of the hard deadline after which discretionary per-PR reconciliation
#: stops. 0.7 preserves the 35/50 ratio the live measurements were taken against.
PR_RECONCILE_SOFT_FRACTION = 0.7
PR_RECONCILE_DEADLINE_SECONDS = (
    TICK_HARD_DEADLINE_SECONDS * PR_RECONCILE_SOFT_FRACTION
)
#: Minimum PRs a pass reconciles even if the soft deadline has passed, so a slow
#: API cannot starve every PR forever and stall reminders entirely. This floor is
#: subordinate to the hard deadline — it is not an exception to it.
PR_RECONCILE_MIN_PER_PASS = 2
#: Ceiling for one `gh api` invocation, used when no budget is active.
GH_API_TIMEOUT_SECONDS = 30
#: A clamped call still gets this much time — below it a request cannot
#: meaningfully complete, so the worst-case tick is
#: TICK_HARD_DEADLINE_SECONDS + this + serialization, not the 30s ceiling.
GH_API_MIN_TIMEOUT_SECONDS = 2.0


class TickBudget:
    """Two-tier wall-clock budget for one poller tick.

    The soft deadline (``exhausted``) stops discretionary per-PR reconciliation
    beyond ``PR_RECONCILE_MIN_PER_PASS``. The hard deadline
    (``hard_exhausted``) stops *all* per-PR work and clamps individual API
    calls, so elapsed time is bounded no matter how slow the API is. Checking
    the budget only between PRs would leave a single 30s call free to carry the
    tick past the framework cap.
    """

    def __init__(
        self,
        deadline_seconds: float = PR_RECONCILE_DEADLINE_SECONDS,
        hard_deadline_seconds: float = TICK_HARD_DEADLINE_SECONDS,
        started_at: float | None = None,
    ) -> None:
        """``started_at`` is when the *tick* began, not when this was built.

        The deadlines are budgets against the framework cap, which is measured
        from process start. Anything that ran before this object exists has
        already spent part of that budget, so it is subtracted here rather than
        silently ignored. ``main()`` passes ``_PROCESS_START``; callers that omit
        it get a budget measured from construction, which is what a test means.
        """
        now = time.monotonic()
        self._start = now
        consumed = max(0.0, now - started_at) if started_at is not None else 0.0
        self._deadline = max(0.0, deadline_seconds - consumed)
        self._hard_deadline = max(0.0, hard_deadline_seconds - consumed)
        #: Pre-budget time already spent, reported with the truncation signal so
        #: a tick squeezed by slow startup is distinguishable from a slow API.
        self.startup_consumed = consumed
        self.truncated: dict[str, int] = {}
        #: True once the hard deadline forced a truncation. The tick must not
        #: advance its `since` watermark in that case — see main().
        self.hard_truncated = False

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def exhausted(self) -> bool:
        return self.elapsed() >= self._deadline

    def hard_remaining(self) -> float:
        return self._hard_deadline - self.elapsed()

    def hard_exhausted(self) -> bool:
        return self.hard_remaining() <= 0.0

    def call_timeout(
        self, ceiling: float = GH_API_TIMEOUT_SECONDS,
    ) -> float | None:
        """Seconds to allow one API call, or ``None`` if it must not be made.

        Returning the *remaining* time rather than flooring it is what makes the
        bound real: a call that starts is guaranteed to finish by the hard
        deadline, so total elapsed is the deadline plus local work — never the
        deadline plus a full call timeout. Below
        ``GH_API_MIN_TIMEOUT_SECONDS`` there is no point starting one, so the
        caller is told to skip instead.
        """
        remaining = self.hard_remaining()
        if remaining < GH_API_MIN_TIMEOUT_SECONDS:
            return None
        return min(ceiling, remaining)

    def affords(self, seconds: float) -> bool:
        """Whether ``seconds`` of work can finish before the hard deadline.

        For transports this module cannot hand a timeout to — the trust
        attestations reach GitHub through ``urllib`` inside ``mimir.pollers`` —
        reserving their worst-case cost up front is what keeps the bound real.
        Passing a timeout across that boundary instead would couple the installed
        skill script to the mimir package version, which is a deploy hazard: the
        two are updated by the same pull, but a skew breaks the whole tick.
        """
        return self.hard_remaining() >= seconds

    def note_truncation(self, pass_name: str, skipped: int) -> None:
        if skipped > 0:
            self.truncated[pass_name] = self.truncated.get(pass_name, 0) + skipped


#: Set for the duration of one tick so `_gh_api` can clamp its subprocess
#: timeout without threading the budget through every pass signature. The
#: poller is a single-shot process, so a module-level handle is the whole
#: lifetime of the run.
_ACTIVE_TICK_BUDGET: "TickBudget | None" = None


def set_active_tick_budget(budget: "TickBudget | None") -> None:
    """Install (or clear) the budget that clamps `_gh_api` call timeouts."""
    global _ACTIVE_TICK_BUDGET
    _ACTIVE_TICK_BUDGET = budget


class _DeadlineExceeded(Exception):
    """A synchronous call outran the wall-clock deadline imposed on it."""


@contextlib.contextmanager
def _wall_clock_deadline(seconds: float):
    """Impose a genuine total deadline on a blocking synchronous call.

    ``urllib.request.urlopen(timeout=...)`` is a *socket-operation* timeout, not a
    total one: a server trickling bytes keeps ``response.read()`` alive as long as
    each individual read makes progress inside the timeout. Reserving the nominal
    timeout as if it bounded the call is therefore unsound — the attestation can
    outlive its reservation and carry the tick past the framework cap.

    ``setitimer`` gives the real thing. Only usable on the main thread of the main
    interpreter, which is what a poller subprocess is; if the platform or thread
    cannot support it the deadline degrades to unenforced rather than raising, and
    the caller keeps the budget check it already had.
    """
    def _fire(_signum, _frame):
        raise _DeadlineExceeded

    try:
        previous = signal.signal(signal.SIGALRM, _fire)
    except (ValueError, AttributeError, OSError):
        yield False
        return
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _refused_window(
    tick_budget: "TickBudget | None", data: object, pass_name: str,
) -> bool:
    """Whether a since-gated listing call came back empty because of the budget.

    These passes rebuild their entire window from one listing call, so a refusal
    is indistinguishable from "nothing new" at the call site — and advancing
    ``last_checked`` past an uncollected window drops those events for good.
    Only the caller knows its window is since-gated, which is why this is checked
    here rather than inside ``_gh_api``.
    """
    if tick_budget is None:
        return False
    explicitly_refused = data is _GH_API_BUDGET_REFUSED
    if not explicitly_refused and (data is not None or not tick_budget.hard_exhausted()):
        return False
    tick_budget.hard_truncated = True
    tick_budget.note_truncation(pass_name, 1)
    return True


def _hard_stop(tick_budget: "TickBudget | None", pass_name: str) -> bool:
    """Hard-deadline check for the since-based passes.

    These keep no per-item dedupe cursor, so they cannot defer an item the way
    the reconcile passes do — truncating them is only safe because a tick that
    sets ``hard_truncated`` leaves its ``last_checked`` watermark alone, so the
    next tick re-scans the same window. There is deliberately no minimum-items
    floor here: past the hard deadline the tick must stop, not make progress.
    """
    if tick_budget is None or not tick_budget.hard_exhausted():
        return False
    tick_budget.hard_truncated = True
    tick_budget.note_truncation(pass_name, 1)
    return True


def _truncate_here(
    tick_budget: "TickBudget | None", reconciled: int, pass_name: str,
) -> bool:
    """Whether this pass should stop reconciling PRs for the rest of the tick.

    ``PR_RECONCILE_MIN_PER_PASS`` is a floor on the *soft* deadline: every pass
    gets that many PRs even on a spent budget, so each one makes forward progress
    and rotation eventually covers the whole set. The floor is **subordinate to
    the hard deadline** — past it, no new per-PR work starts at all, because a
    guaranteed minimum that can still start a 30s call is not a bound.
    """
    if tick_budget is None:
        return False
    if tick_budget.hard_exhausted():
        # Deliberately does not set ``hard_truncated``: these passes defer a
        # skipped PR through their own dedupe cursor (a preserved prior entry,
        # or ``collection_complete=False``), so the global ``last_checked``
        # watermark can still advance. Only the since-based passes, which have
        # no per-item state to defer into, need it held.
        tick_budget.note_truncation(pass_name, 1)
        return True
    if reconciled < PR_RECONCILE_MIN_PER_PASS:
        return False
    if not tick_budget.exhausted():
        return False
    tick_budget.note_truncation(pass_name, 1)
    return True


def _rotate(items: list, offset: int) -> list:
    """Rotate so successive ticks start where the previous one stopped."""
    if not items:
        return items
    start = offset % len(items)
    return items[start:] + items[:start]


CHANGES_REQUESTED_REMINDER_INTERVAL = timedelta(minutes=60)
CHANGES_REQUESTED_REMINDER_SLACK = timedelta(minutes=1)
CHANGES_REQUESTED_GAVE_UP_BACKSTOP = timedelta(hours=24)
# These fragments come from the checkout controller's ToolPolicyRefusal text.
# Only refusals whose blockers are removed by lease expiry/reconciliation belong
# here; unknown reasons must retain the permanent-fault backoff.
_SELF_CLEARING_REFUSAL_REASON_FRAGMENTS = (
    "superseded PR checkout lease has retained work; refusing release:",
    "unpublished PR checkout lease candidates include another scope; refusing reuse:",
    "divergent unpublished PR checkout lease candidates; refusing implicit selection:",
    "PR checkout lease collision",
    "PR checkout lease recovery scope mismatch at",
)
_REFUSAL_SELF_CLEARING = "self_clearing"
_REFUSAL_OPERATOR_GATED = "operator_gated"
MERGEABILITY_RETRY_INTERVAL = timedelta(hours=1)
CI_DELIVERY_RETRY_INTERVAL = timedelta(minutes=5)
CI_FAILURE_CONCLUSIONS = frozenset({
    "failure", "timed_out", "startup_failure", "action_required",
})
_DELIVERY_RECEIPTS_DIR = ".delivery-receipts"
_DELIVERY_CLAIMS_DIR = ".delivery-claims"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_refusal_reasons(reasons: list[str]) -> str:
    """Classify known lease contention as transient, failing closed otherwise."""
    if reasons and all(
        any(fragment in reason for fragment in _SELF_CLEARING_REFUSAL_REASON_FRAGMENTS)
        for reason in reasons
    ):
        return _REFUSAL_SELF_CLEARING
    return _REFUSAL_OPERATOR_GATED


def _load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cursor(cursor: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"cursor.{os.getpid()}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cursor, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CURSOR_FILE)
        directory_fd = os.open(STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _delivery_key(repo: str, number: int, head_sha: str, failures: list[dict]) -> str:
    failure_set = sorted(
        f"{check.get('name', '')}\0{check.get('conclusion', '')}"
        for check in failures
    )
    digest = hashlib.sha256(json.dumps(failure_set).encode()).hexdigest()
    return f"github-pr-ci:{repo.lower()}:{number}:{head_sha.lower()}:{digest}"


def _delivery_receipt_exists(delivery_key: str) -> bool:
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    return (STATE_DIR / _DELIVERY_RECEIPTS_DIR / digest).is_file()


def _claim_delivery(delivery_key: str, now: datetime) -> bool:
    """Atomically claim an emit so overlapping poll processes cannot duplicate it."""
    directory = STATE_DIR / _DELIVERY_CLAIMS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    path = directory / digest
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                claimed_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                return False
            if now - claimed_at < CI_DELIVERY_RETRY_INTERVAL:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        os.close(fd)
        os.utime(path, (now.timestamp(), now.timestamp()))
        return True
    return False


def _remove_delivery_artifacts(delivery_key: object) -> None:
    if not isinstance(delivery_key, str) or not delivery_key:
        return
    digest = hashlib.sha256(delivery_key.encode()).hexdigest()
    for directory in (_DELIVERY_RECEIPTS_DIR, _DELIVERY_CLAIMS_DIR):
        try:
            (STATE_DIR / directory / digest).unlink()
        except FileNotFoundError:
            pass


def _coerce_review_requests(value: object) -> dict[str, int]:
    """Coerce a per-repo review-request cursor entry to ``{pr_key: attempts}``.

    chainlink #299 changed the shape of the review-request cursor from a
    bare ``list`` of "already-emitted" PR-number strings (the pre-#299
    emit-once-on-transition model) to ``{pr_key: attempt_count}`` so the
    poller can re-emit a still-pending request up to a cap. This migrates
    the old format on first load after the upgrade:

    * ``list`` → ``{key: 1}`` — treat each previously-emitted request as
      one recorded attempt, so a request that's still open becomes
      eligible for the retry path rather than re-firing from scratch.
    * ``dict`` → kept, filtered to ``str``-keyed non-negative ``int``
      values (defends against a hand-edited / corrupted cursor).
    * anything else → ``{}``.
    """
    if isinstance(value, dict):
        out: dict[str, int] = {}
        for k, v in value.items():
            # bool is an int subclass — exclude it explicitly so a stray
            # ``true`` doesn't read as attempts=1.
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[k] = v
        return out
    if isinstance(value, list):
        return {str(k): 1 for k in value if isinstance(k, (str, int)) and not isinstance(k, bool)}
    return {}


def _review_recovery_state(
    repo: str,
    number: str,
    *,
    event_types: frozenset[str] = _REVIEW_TURN_EVENT_TYPES,
    after: str = "",
    head_sha: str = "",
    pending_before: str = "",
) -> tuple[int, bool, bool, bool, list[str], str, list[str], int] | None:
    """Return charged/pending/refusal details from framework recovery state.

    ``None`` means recovery state is unavailable, for example when this script is
    run directly outside the poller framework. Completed turns are removed by
    reconciliation; failed turns remain when generic failed-turn recovery is
    disabled, as it is for github-activity.
    """
    path = STATE_DIR / _RECOVERY_STATE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    inflight = data.get("inflight") if isinstance(data, dict) else None
    if not isinstance(inflight, dict):
        return None

    started = 0
    pending = False
    found = False
    canonical = False
    attempt_reasons: list[str] = []
    latest_refusal_at = ""
    refusal_reasons: list[str] = []
    self_clearing_refusals = 0
    for entry in inflight.values():
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        extra = event.get("extra") if isinstance(event, dict) else None
        items = extra.get("items") if isinstance(extra, dict) else None
        if not isinstance(items, list):
            continue
        matching_types = {
            item.get("event_type")
            for item in items
            if (
                isinstance(item, dict)
                and item.get("event_type") in event_types
                and item.get("repo") == repo
                and str(item.get("number")) == number
                and (not head_sha or item.get("head_sha") == head_sha)
            )
        }
        if not matching_types:
            continue
        found = True
        canonical = canonical or bool(matching_types & {"pr_opened", "pr_synchronize"})
        outcome_at = entry.get("last_outcome_at")
        if after and isinstance(outcome_at, str) and outcome_at <= after:
            continue
        attempts = entry.get("attempts", 0)
        attempts = (
            attempts
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else 0
        )
        if outcome_at:
            if entry.get("outcome_disposition") == "exempt_hard_refusal":
                raw_reason = entry.get("outcome_reason")
                reason = (
                    raw_reason if isinstance(raw_reason, str) else "hard_boundary_refusal"
                )
                if _classify_refusal_reasons([reason]) == _REFUSAL_SELF_CLEARING:
                    self_clearing_refusals += 1
                if isinstance(outcome_at, str) and outcome_at >= latest_refusal_at:
                    latest_refusal_at = outcome_at
                    refusal_reasons = [reason]
                continue
            # Pre-fix persisted failures may still say attempts=0. The durable
            # outcome proves that turn started even when the old counter did not.
            started += max(attempts, 1)
            raw_reasons = entry.get("attempt_reasons")
            if isinstance(raw_reasons, list):
                attempt_reasons.extend(str(reason)[:240] for reason in raw_reasons)
            else:
                attempt_reasons.append(str(entry.get("outcome_reason") or "turn_failed")[:240])
        else:
            queued_at = entry.get("enqueued_at") or entry.get("stashed_at")
            pending = pending or not (
                pending_before
                and isinstance(queued_at, str)
                and queued_at <= pending_before
            )
    return (
        started, pending, found, canonical, attempt_reasons,
        latest_refusal_at, refusal_reasons, self_clearing_refusals,
    )


def _resolve_token() -> str:
    """Get a GitHub PAT from env or ``gh auth token``."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


class _GhApiBudgetRefused:
    """Marker returned when the tick budget prevents an API call from starting."""


_GH_API_BUDGET_REFUSED = _GhApiBudgetRefused()


def _gh_api(endpoint: str, token: str) -> list | dict | None | _GhApiBudgetRefused:
    """Call ``gh api <endpoint> --paginate`` and return parsed JSON.

    Returns ``None`` on an ordinary API error and a private marker when the tick
    budget refuses the call. Since-gated callers must distinguish those cases so
    they do not advance their watermark over a window they never collected.
    """
    try:
        env = {**os.environ, "GH_TOKEN": token} if token else None
        budget = _ACTIVE_TICK_BUDGET
        if budget is None:
            timeout: float = float(GH_API_TIMEOUT_SECONDS)
        else:
            allowed = budget.call_timeout()
            if allowed is None:
                # Past the hard deadline. Returning None is the same shape every
                # caller already handles for an API failure, so cursor entries
                # are preserved rather than rebuilt from a partial view.
                #
                # Deliberately does NOT mark the tick truncated: only the
                # caller knows whether its window is since-gated. Marking here
                # froze `last_checked` when a *reconcile* pass had a listing call
                # refused, and those passes defer through their own cursors. The
                # since-based passes check for this refusal themselves via
                # ``_refused_window`` below.
                budget.note_truncation("gh_api_refused", 1)
                print(
                    f"gh api {endpoint} skipped: tick budget exhausted",
                    file=sys.stderr,
                )
                return _GH_API_BUDGET_REFUSED
            timeout = allowed
        result = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        if result.returncode != 0:
            print(
                f"gh api {endpoint} returned {result.returncode}: "
                f"{result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as exc:
        print(f"gh api {endpoint} failed: {exc}", file=sys.stderr)
    return None


def _truncate(text: str, n: int = BODY_PREVIEW_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


#: Event types where a PR review action is expected from the agent.
#: For these, the framework appends a short submission rule to the
#: emitted prompt so the reasoning-before-Skill-loads issue (Mimir's
#: post-#234 investigation) doesn't leave the review unsubmitted —
#: rule arrives in context before the model's reasoning commits.
REVIEW_NEEDED_EVENT_TYPES = frozenset({
    "pr_opened",                # brand-new PR
    "pr_synchronize",           # push to an existing PR (re-review)
    "pr_review_requested",      # the agent's login was added to
                                # ``requested_reviewers`` on an open PR
})

_REVIEW_SUBMISSION_RULE = (
    "\n\n──── REVIEW SUBMISSION RULE ────\n"
    "This event needs a review. After drafting your review prose, "
    "you MUST submit it via `gh pr review` (or "
    "`pull_request_review_write` MCP tool). Review prose alone — "
    "left in turn output and never sent — is a non-review. The "
    "trusted-service shell executes one argv with no shell: use one "
    "command per shell call and never use cd, &&, semicolons, pipes, "
    "redirects, heredocs, or command substitution. Read `fetch_url` cache "
    "paths with `read_file` (offset/limit for a line range) or bounded "
    "`grep` before_context/after_context; shell slicing forms and direct `curl` "
    "are intentionally refused because they bypass the bounded file/egress tools. "
    "/review skill spells out the full flow; this rule is restated "
    "here so it's present in your context before the Skill call "
    "fires."
)

# Scratch cleanup is NOT instructed here. A same-turn `rm -rf` of the event's
# scratch clone is behaviorally unreachable: the agent's action boundary makes
# every delete under /mimir-home escalate-first, and a poller event is not
# operator approval — so a conforming turn would have to stop and ask. The
# scheduler's scratch janitor (harness code, not bound by that rule) is the
# mechanism instead; see mimir/scratch_janitor.py (MIMIR_SCRATCH_TTL_DAYS).


#: Marker dict the framework reads at turn finalization. When the
#: turn's tool_calls don't match any of these tool names / Bash
#: substrings, ``signal_on_missing`` is emitted into events.jsonl
#: where ``feedback._EVENT_RULES`` classifies it algedonically.
#: Lives on the poller side (not in agent.py) so the policy "what
#: counts as 'review submitted'" belongs to this skill — Mimir's
#: PR #234 / #235 nit about coupling.
_REVIEW_EXPECTED_TOOL_CALL: dict = {
    "tool_names": [
        # MCP path (GitHub MCP server)
        "pull_request_review_write",
        "submit_pending_pull_request_review",
        "mcp__claude_ai_GitHub_remote__pull_request_review_write",
        "mcp__claude_ai_GitHub_remote__submit_pending_pull_request_review",
    ],
    "bash_substrings": [
        # /review skill's documented path. Trailing space discriminates
        # from ``gh pr review-comment`` (the standalone-comment
        # subcommand), which is NOT a review submission — Mimir's PR
        # #236 review nit.
        "gh pr review ",
    ],
    "signal_on_missing": "poller_review_missed_submission",
}


#: Mimir's canonical env-boolean alphabet, mirroring ``_ENV_BOOL_TRUTHY`` /
#: ``_ENV_BOOL_FALSY`` in ``mimir/config.py``. It is duplicated rather than
#: imported because this poller must run under an interpreter that cannot import
#: ``mimir`` at all (see tests/test_optional_skill_poller_entrypoints.py). A
#: duplicated parser is exactly the kind of second copy that drifts, so
#: tests/test_github_poller_prompt.py compares both sets against
#: ``mimir.config`` whenever it IS importable and fails if they diverge.
_ENV_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})
_ENV_FALSY = frozenset({"0", "false", "no", "off", "n"})


def _env_flag(name: str, default: bool = False) -> bool:
    """Read one env var with Mimir's canonical boolean semantics.

    Unset, empty, and unrecognised values all return *default*, matching
    ``config._env_bool`` so a typo cannot silently flip the flag.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in _ENV_TRUTHY:
        return True
    if normalized in _ENV_FALSY:
        return False
    return default


def _verification_guidance() -> str:
    """How a remediation turn should verify its fix, given this deployment.

    ``repo_test`` is only registered when ``MIMIR_CODING_ENABLED`` is true, and
    that setting defaults to false (``mimir/config.py`` ``coding_enabled``), so
    naming the tool unconditionally would point a default deployment at
    something it does not have. Selector wording stays runner-neutral for the
    same reason: ``project_tests`` passes ``path::node_id`` through to whatever
    runner ``worklink.yaml`` configures, and only pytest-style runners accept
    that form.
    """
    if not _env_flag("MIMIR_CODING_ENABLED"):
        return (
            "verify the fix with whatever this deployment provides, and do not "
            "present changes as verified if you could not run its tests — say "
            "plainly what you were unable to check"
        )
    return (
        "verify with the repo_test tool, which runs the deployment's own "
        "configured test command. Selectors are repo-relative paths that must "
        "already exist and must not be symlinks, and carry no flags; "
        "`path::node_id` works only when the configured runner accepts that "
        "form, so prefer plain paths — or omit selectors to run the whole suite"
    )


def _load_review_skill_body(mimir_home: str, skill_path_override: str = "") -> str:
    """Load and return the review skill's SKILL.md body for inlining.

    Returns ``""`` (empty) on any failure — the submission rule alone
    is sufficient when the full skill can't be loaded; we'd rather
    surface a small in-prompt note than crash the poll.

    ``mimir_home`` is the agent home root; ``skill_path_override`` is
    an absolute path that wins if non-empty (operator escape hatch
    for non-standard layouts).
    """
    override = skill_path_override.strip()
    if override:
        candidates = [override]
    elif mimir_home:
        home = Path(mimir_home)
        # mimir resolves skills from two locations, operator-first:
        # ``<home>/skills/`` (operator-installed) then
        # ``<home>/.mimir_builtin_skills/`` (the bundled refresh target).
        # ``review`` is a BUNDLED skill (mimir/skills/review/), so on a
        # normal install it lives in ``.mimir_builtin_skills/`` — checking
        # only ``skills/`` (or, pre-#516, ``.claude/skills/``) missed it,
        # so the preload silently no-op'd on every real deployment
        # (chainlink #299 follow-up). ``.claude/skills/`` is the Claude
        # Code convention; the framework migrates it into ``skills/`` at
        # startup, so it isn't checked here.
        candidates = [
            str(home / "skills" / "review" / "SKILL.md"),
            str(home / ".mimir_builtin_skills" / "review" / "SKILL.md"),
        ]
    else:
        return ""
    for candidate in candidates:
        try:
            body = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            return "\n\n──── /review SKILL.md (pre-loaded) ────\n" + body
    _eprint(
        "github-poller: review-skill preload disabled — none readable: "
        + ", ".join(candidates)
    )
    return ""


def _eprint(*args: object, **kwargs: object) -> None:
    """Stderr printer (captured by framework into poller_stderr)."""
    print(*args, file=sys.stderr, **kwargs)


def _emit(prompt: str, **extras: object) -> None:
    """One JSONL event line — framework parses + delivers as
    AgentEvent. ``source_platform`` flows through for prompt
    rendering.

    For ``event_type`` values in ``REVIEW_NEEDED_EVENT_TYPES`` the
    function appends a submission rule (always) and, when
    ``MIMIR_GITHUB_PRELOAD_REVIEW_SKILL`` is set to ``1``/``true``,
    inlines the full review SKILL.md body. The emitted event also
    carries an ``expected_tool_call`` marker dict so the framework's
    post-turn check (``agent.py::_turn_matched_expected_tool_call``)
    can detect "wrote a review, didn't submit" and emit
    ``poller_review_missed_submission`` algedonically.
    """
    event_type = extras.get("event_type")
    if isinstance(event_type, str) and event_type.startswith("pr_"):
        extras.setdefault("subject_type", "pull_request")
    if isinstance(event_type, str) and event_type in REVIEW_NEEDED_EVENT_TYPES:
        related_comment = extras.pop("related_comment", "")
        if isinstance(related_comment, str) and related_comment:
            prompt = f"{prompt}\n\nRelated PR comment:\n{related_comment}"
        prompt = prompt + _REVIEW_SUBMISSION_RULE
        if _env_flag("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL"):
            body = _load_review_skill_body(
                os.environ.get("MIMIR_HOME", ""),
                os.environ.get("MIMIR_GITHUB_REVIEW_SKILL_PATH", ""),
            )
            if body:
                prompt = prompt + body
        # Generic framework hook (Mimir PR #234/#235 follow-up): the
        # poller declares which tool calls satisfy "review submitted"
        # and which signal to emit when none of them fired. agent.py
        # reads this marker at turn finalization and emits the
        # declared signal algedonically. The list lives here (in the
        # skill closest to the domain) rather than hardcoded in
        # agent.py so adding a new poller's expectation is a skill-
        # side change.
        # Per-PR marker (chainlink #308): a PR-specific ``gh pr review
        # <number>`` substring (plus the PR url) lets the framework's
        # per-item missed-submission check attribute WHICH review wasn't
        # submitted, so a duplicate review of one PR can't mask an
        # unreviewed sibling in the same batch. ``ref`` is surfaced in the
        # signal. Falls back to the generic marker when the number is
        # unavailable. (The MCP ``tool_names`` path isn't PR-attributable —
        # it matches by name — but mimir-carreira reviews via ``gh``.)
        marker = dict(_REVIEW_EXPECTED_TOOL_CALL)
        number = extras.get("number")
        url = extras.get("url")
        repo = extras.get("repo")
        reviewer = extras.get("requested_reviewer") or os.environ.get(
            "MIMIR_GITHUB_SELF_LOGIN", ""
        ).strip()
        head_sha = extras.get("head_sha") or extras.get("new_head")
        pr_substrings: list[str] = []
        if number is not None:
            pr_substrings.append(f"gh pr review {number}")
            if isinstance(repo, str) and repo:
                pr_substrings.append(f"gh pr review --repo {repo} {number}")
                pr_substrings.append(f"gh pr review -R {repo} {number}")
        if isinstance(url, str) and url:
            pr_substrings.append(url)
        if pr_substrings:
            marker["bash_substrings"] = pr_substrings
            marker["ref"] = url or f"#{number}"
        if isinstance(repo, str) and repo:
            marker["repo"] = repo
        if number is not None:
            marker["number"] = number
        if isinstance(reviewer, str) and reviewer:
            marker["reviewer"] = reviewer
        if isinstance(head_sha, str) and head_sha:
            marker["head_sha"] = head_sha
        extras["expected_tool_call"] = marker
    event = {
        "poller": POLLER_NAME,
        "source_platform": "github",
        "prompt": prompt,
        **extras,
    }
    print(json.dumps(event), flush=True)


def _emit_signal(signal_type: str, **extras: object) -> None:
    """One signal-shaped JSONL line (chainlink #299).

    Unlike :func:`_emit` (which writes a ``prompt`` → the framework builds
    an AgentEvent and spawns a turn), a signal record carries ``signal``
    instead of ``prompt``: ``mimir/pollers.py`` routes it to
    ``events.jsonl`` via ``log_event`` WITHOUT spawning a turn, where
    ``feedback.classify`` surfaces recognized types — including the
    ``*_gave_up`` suffix — in the next turn's negative algedonic block.

    Used for "give up" notifications that should be VISIBLE but must not
    trigger more work — re-spawning a turn after the retry budget is
    exhausted would just burn another likely-failing turn. ``extras``
    (repo / number / url / attempts) flow through to the event payload
    for the renderer; ``poller`` is re-stamped by the framework.
    """
    print(
        json.dumps({"poller": POLLER_NAME, "signal": signal_type, **extras}),
        flush=True,
    )


#: Ceiling for one trust lookup, matching `_github_api_attestation`'s own
#: `timeout` default in `mimir.pollers`. That default is a socket-operation
#: timeout, not a total one, so it is enforced here with a real wall-clock
#: deadline rather than trusted as a bound.
TRUST_ATTESTATION_TIMEOUT_SECONDS = 10.0


def _pr_author_is_trusted(
    repo: str,
    number: int,
    url: str,
    token: str,
    trust_cache: dict[tuple[str, object], object],
    *,
    tick_budget: "TickBudget | None" = None,
) -> bool | None:
    """Resolve PR-author trust from GitHub and cache it for this poll cycle.

    Returns ``None`` when the tick has no budget left or a server attestation is
    unavailable. That is deliberately distinct from ``False``: an unresolved
    author must be *skipped*, not classified, because ``False`` routes the PR through
    ``_surface_untrusted_pr_once`` which emits a signal and records the verdict
    as already-surfaced. Failing closed here would permanently mislabel a
    trusted contributor's PR because the poller ran out of time.

    This path reaches GitHub through `urllib` in ``mimir.pollers``, not through
    ``_gh_api``'s subprocess, so it needs its own budget plumbing — bounding only
    the `gh api` transport left this one free to overrun the tick.
    """
    def _allowance() -> float | None:
        """Wall-clock seconds this lookup may take, or None to skip it."""
        if tick_budget is None:
            return TRUST_ATTESTATION_TIMEOUT_SECONDS
        return tick_budget.call_timeout(
            ceiling=TRUST_ATTESTATION_TIMEOUT_SECONDS,
        )

    # A cached author costs nothing and is never gated.
    author_key = (repo, number)
    if author_key not in trust_cache:
        allowed = _allowance()
        if allowed is None:
            return None
        try:
            with _wall_clock_deadline(allowed):
                resolved = _github_content_author(
                    repo,
                    {"event_type": "pr_opened", "url": url},
                    token,
                )
        except _DeadlineExceeded:
            # Unresolved, not untrusted — see the tri-state note above. Do not
            # cache: the next tick should retry rather than inherit a verdict
            # produced by a timeout.
            return None
        if resolved is None:
            # Transport failure or malformed response is unresolved, not an
            # authoritative untrusted verdict. Do not cache it; retry next tick.
            return None
        trust_cache[author_key] = resolved
    author = trust_cache[author_key]
    trust_key = (repo, author if isinstance(author, str) else "")
    if trust_key not in trust_cache:
        allowed = _allowance()
        if allowed is None:
            return None
        try:
            with _wall_clock_deadline(allowed):
                trusted = _github_author_is_trusted(repo, author, token)
        except _DeadlineExceeded:
            return None
        if trusted is None:
            # Preserve the transport's unavailable state separately from False.
            return None
        trust_cache[trust_key] = trusted
    return trust_cache[trust_key] is True


def _review_requested(pr: dict, me: str) -> bool:
    return bool(me) and any(
        isinstance(reviewer, dict) and reviewer.get("login") == me
        for reviewer in (pr.get("requested_reviewers") or [])
    )


def _pr_scope_fields(pr: dict, repo: str) -> dict[str, object]:
    """The PR head/base snapshot the framework needs to issue a PR scope.

    Without every one of these fields mimir/access_control.py refuses to issue
    a RepoPRActionScope, so the agent cannot review, comment on, or check out
    the PR it was woken for -- and the refusal it sees names the live-discovery
    operator gate rather than the missing snapshot.

    ``head_remote`` distinguishes a same-repo branch from a fork. Remediation
    scopes additionally require "origin", so a same-repo PR must report it.
    """
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = ((head.get("repo") or {}).get("full_name") or "")
    return {
        "head_repo": head_repo or None,
        "head_remote": "origin" if head_repo.lower() == repo.lower() else "source",
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "base_ref": base.get("ref"),
        "base_sha": base.get("sha"),
    }


def _emit_pr_synchronize(
    repo: str,
    number: int,
    title: str,
    url: str,
    previous_head: str,
    current_head: str,
    token: str,
    reviewer: str,
    *,
    pr: dict | None = None,
    related_comment: str = "",
) -> bool:
    compare = _gh_api(
        f"repos/{repo}/compare/{previous_head}...{current_head}", token,
    )
    commits: list = []
    total_commits = 0
    if isinstance(compare, dict):
        commits = compare.get("commits") or []
        total_commits = compare.get("ahead_by") or len(commits)
    head_commit = commits[-1] if commits else {}
    push_author = (
        (head_commit.get("author") or {}).get("login")
        or (head_commit.get("committer") or {}).get("login")
    )
    if total_commits and commits:
        subjects = [
            (commit.get("commit") or {}).get("message", "").split("\n")[0][:72]
            for commit in commits[:3]
        ]
        bullets = "\n".join(f"  • {subject}" for subject in subjects if subject)
        remaining = total_commits - sum(1 for subject in subjects if subject)
        commit_block = f"{total_commits} commit(s):\n{bullets}"
        if remaining > 0:
            commit_block += f"\n  • … ({remaining} more)"
    else:
        commit_block = "(commit details unavailable)"
    prompt = (
        f"PR #{number} updated on {repo}: {title} "
        f"(by @{push_author or 'unknown'})\n"
        f"{commit_block}\n"
        f"Previous head: {previous_head[:8]}, new head: {current_head[:8]}\n{url}"
    )
    return _emit_pr_review_needed(
        prompt,
        token=token,
        reviewer=reviewer,
        event_type="pr_synchronize",
        repo=repo,
        number=number,
        url=url,
        previous_head=previous_head,
        new_head=current_head,
        author=push_author,
        **(
            _pr_scope_fields(pr, repo) if pr is not None
            else {"head_sha": current_head}
        ),
        related_comment=related_comment,
    )


def _surface_untrusted_pr_once(
    repo: str,
    number: int,
    url: str,
    surfaced_untrusted: set[str],
) -> int:
    key = str(number)
    if key in surfaced_untrusted:
        return 0
    _emit_signal(
        "pr_auto_review_skipped_untrusted_author",
        repo=repo,
        number=number,
        url=url,
    )
    surfaced_untrusted.add(key)
    return 1


# ─── per-resource checks ──────────────────────────────────────────────


def _check_issues(
    repo: str, since: str, token: str, me: str,
    *, tick_budget: "TickBudget | None" = None,
) -> int:
    """New issues (NOT PRs — GitHub's /issues endpoint returns both;
    we filter PRs out via the ``pull_request`` field)."""
    data = _gh_api(
        f"repos/{repo}/issues?state=open&since={since}"
        f"&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
        _refused_window(tick_budget, data, "issues_window")
        return 0
    count = 0
    for issue in data:
        if issue.get("pull_request"):
            continue  # PRs handled by _check_prs
        if me and issue.get("user", {}).get("login") == me:
            continue
        if (issue.get("created_at", "") or "") <= since:
            continue
        author = issue.get("user", {}).get("login", "unknown")
        number = issue.get("number")
        title = issue.get("title", "")
        url = issue.get("html_url", "")
        body = _truncate(issue.get("body") or "")
        prompt_parts = [
            f"New issue on {repo}: #{number} {title} (by @{author})",
        ]
        if body:
            prompt_parts.append(body)
        prompt_parts.append(url)
        _emit("\n".join(prompt_parts), event_type="issue_opened",
              repo=repo, number=number, url=url, author=author)
        count += 1
    return count


def _check_prs(
    repo: str,
    since: str,
    token: str,
    me: str,
    trust_cache: dict[tuple[str, object], object] | None = None,
    surfaced_untrusted: set[str] | None = None,
    review_needed_pr_numbers: set[str] | None = None,
    review_context: dict[str, str] | None = None,
    tick_budget: "TickBudget | None" = None,
) -> int:
    """New pull requests."""
    data = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
        _refused_window(tick_budget, data, "prs_window")
        return 0
    trust_cache = trust_cache if trust_cache is not None else {}
    surfaced_untrusted = surfaced_untrusted if surfaced_untrusted is not None else set()
    review_needed_pr_numbers = (
        review_needed_pr_numbers if review_needed_pr_numbers is not None else set()
    )
    review_context = review_context if review_context is not None else {}
    count = 0
    for pr in data:
        if me and pr.get("user", {}).get("login") == me:
            continue
        if (pr.get("created_at", "") or "") <= since:
            continue
        author = pr.get("user", {}).get("login", "unknown")
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        # Explicit requests are handled by _check_pr_pushes regardless of
        # author. Do not also report them as skipped automatic reviews.
        if _review_requested(pr, me):
            continue
        trusted = _pr_author_is_trusted(
            repo, number, url, token, trust_cache, tick_budget=tick_budget,
        )
        if trusted is None:
            # No budget left to resolve trust. Skip without classifying, and
            # hold the watermark so this PR is reconsidered next tick.
            if tick_budget is not None:
                tick_budget.hard_truncated = True
                tick_budget.note_truncation("prs_trust", 1)
            continue
        if not trusted:
            count += _surface_untrusted_pr_once(
                repo, number, url, surfaced_untrusted,
            )
            continue
        body = _truncate(pr.get("body") or "")
        prompt_parts = [f"New PR on {repo}: #{number} {title} (by @{author})"]
        if body:
            prompt_parts.append(body)
        prompt_parts.append(url)
        emitted = _emit_pr_review_needed(
            "\n".join(prompt_parts),
            token=token,
            reviewer=me,
            event_type="pr_opened",
            repo=repo,
            number=number,
            url=url,
            author=author,
            **_pr_scope_fields(pr, repo),
            related_comment=review_context.get(str(number), ""),
        )
        if emitted:
            count += 1
            review_needed_pr_numbers.add(str(number))
    return count



def _collect_issue_comment_context(
    repo: str,
    since: str,
    token: str,
    me: str,
) -> tuple[list[dict] | None, dict[str, str]]:
    """Fetch comments once and collect recent PR prose for review prompts."""
    data = _gh_api(
        f"repos/{repo}/issues/comments?since={since}"
        f"&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
        return None, {}
    context: dict[str, str] = {}
    for comment in data:
        if me and comment.get("user", {}).get("login") == me:
            continue
        if (comment.get("created_at", "") or "") <= since:
            continue
        url = comment.get("html_url", "")
        if "/pull/" not in url:
            continue
        issue_url = comment.get("issue_url", "")
        issue_num = issue_url.rstrip("/").split("/")[-1] if issue_url else "?"
        author = comment.get("user", {}).get("login", "unknown")
        body = _truncate(comment.get("body") or "")
        rendered = f"@{author}: {body}\n{url}"
        if issue_num in context:
            context[issue_num] = f"{context[issue_num]}\n\n{rendered}"
        else:
            context[issue_num] = rendered
    return data, context

def _check_issue_comments(
    repo: str,
    since: str,
    token: str,
    me: str,
    *,
    review_needed_pr_numbers: set[str] | None = None,
    comments: list[dict] | None = None,
    tick_budget: "TickBudget | None" = None,
) -> int:
    """New issue + PR conversation comments.

    The ``/repos/{repo}/issues/comments`` endpoint covers both issues and
    pull requests.  PR comments need two extra guards:

    * a comment discovered after its PR has closed or merged is terminal history,
      not a fresh work signal;
    * when the same poll already emitted a review-needed event for that PR (for
      example ``pr_synchronize``), the comment is supporting context for that one
      review, not a second independent turn.

    Parent type comes from the authoritative issue resource's ``pull_request``
    marker rather than the comment's presentation URL. Ordinary issue comments
    keep the existing edge-triggered behaviour regardless of issue state. If the
    live parent lookup fails, fail open and emit the comment rather than silently
    losing a potentially actionable signal.
    """
    data = comments
    if data is None:
        data = _gh_api(
            f"repos/{repo}/issues/comments?since={since}"
            f"&sort=created&direction=desc",
            token,
        )
    if not isinstance(data, list):
        return 0
    count = 0
    parent_cache: dict[str, dict | None] = {}
    for comment in data:
        if _hard_stop(tick_budget, "issue_comments"):
            break
        if me and comment.get("user", {}).get("login") == me:
            continue
        if (comment.get("created_at", "") or "") <= since:
            continue
        author = comment.get("user", {}).get("login", "unknown")
        body = _truncate(comment.get("body") or "")
        url = comment.get("html_url", "")
        issue_url = comment.get("issue_url", "")
        issue_num = (
            issue_url.rstrip("/").split("/")[-1] if issue_url else "?"
        )
        presentation_is_pr = "/pull/" in url
        if presentation_is_pr and issue_num in (review_needed_pr_numbers or set()):
            continue
        if issue_num not in parent_cache:
            parent_cache[issue_num] = _gh_api(
                f"repos/{repo}/issues/{issue_num}", token,
            )
        parent = parent_cache[issue_num]
        is_pr_comment = isinstance(parent, dict) and bool(parent.get("pull_request"))
        if is_pr_comment:
            if issue_num in (review_needed_pr_numbers or set()):
                continue
            if parent.get("state") != "open":
                continue
        prompt = (
            f"New comment on {repo} #{issue_num} by @{author}: {body}\n{url}"
        )
        if is_pr_comment or presentation_is_pr:
            emitted = _emit_pr_review_needed(
                prompt,
                token=token,
                reviewer=me,
                activity_at=comment.get("created_at"),
                event_type="issue_comment",
                subject_type="pull_request",
                repo=repo,
                number=issue_num,
                url=url,
                author=author,
            )
        else:
            _emit(prompt, event_type="issue_comment",
                  repo=repo, number=issue_num, url=url, author=author)
            emitted = True
        count += int(emitted)
    return count


def _check_pr_review_comments(
    repo: str, since: str, token: str, me: str,
    *, tick_budget: "TickBudget | None" = None,
) -> int:
    """New PR review comments — these are INLINE diff comments,
    distinct from issue/PR conversation comments. The bulk of code
    review feedback lives here. Open-strix's poller missed this
    endpoint; chainlink #3's expansion adds it."""
    data = _gh_api(
        f"repos/{repo}/pulls/comments?since={since}"
        f"&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
        _refused_window(tick_budget, data, "review_comments_window")
        return 0
    count = 0
    for comment in data:
        if me and comment.get("user", {}).get("login") == me:
            continue
        if (comment.get("created_at", "") or "") <= since:
            continue
        author = comment.get("user", {}).get("login", "unknown")
        body = _truncate(comment.get("body") or "")
        url = comment.get("html_url", "")
        pr_url = comment.get("pull_request_url", "")
        pr_num = pr_url.rstrip("/").split("/")[-1] if pr_url else "?"
        path = comment.get("path", "")
        location = f" on {path}" if path else ""
        prompt = (
            f"New PR review comment on {repo} #{pr_num} "
            f"by @{author}{location}: {body}\n{url}"
        )
        count += int(_emit_pr_review_needed(
            prompt,
            token=token,
            reviewer=me,
            activity_at=comment.get("created_at"),
            event_type="pr_review_comment",
            repo=repo,
            number=pr_num,
            url=url,
            path=path,
            author=author,
        ))
    return count


def _check_pr_pushes(
    repo: str,
    token: str,
    me: str,
    pr_heads: dict[str, str],
    pr_review_requests: dict[str, int] | None = None,
    trust_cache: dict[tuple[str, object], object] | None = None,
    surfaced_untrusted: set[str] | None = None,
    review_needed_pr_numbers: set[str] | None = None,
    review_context: dict[str, str] | None = None,
    tick_budget: "TickBudget | None" = None,
) -> tuple[int, dict[str, str], dict[str, int]]:
    """Detect new commits pushed to existing open PRs AND new
    review-requests addressed to ``me`` on those same PRs.

    Different signature from the sibling checks: takes the per-repo
    cursors directly (``pr_heads`` for push-detection,
    ``pr_review_requests`` for review-request-detection) and returns
    them rebuilt from the current ``state=open`` snapshot. The
    cleanup model is "rebuild on every poll" — closed/merged PRs and
    PRs in repos no longer in the watch list naturally drop out
    because they're never copied into the new cursor.

    Return shape: ``(emit_count, new_pr_heads, new_review_requests)``.

    ── Push detection ──
    First sighting of a PR: record its head sha, do NOT emit.
    ``_check_prs`` already fires ``pr_opened`` for genuinely-new PRs;
    the first poll after this feature ships would otherwise bulk-fire
    on every existing open PR, which is noise.

    Subsequent sighting with a different head sha: emit a
    ``pr_synchronize`` event and record the new sha. This catches
    force-pushes too — a rebase that doesn't change the diff vs.
    base will still advance ``head.sha``, so we'll fire on it. That's
    a known false-positive; the alternative (compare diffs) is too
    expensive to run on every poll.

    ── Review-request detection (state-reconciling re-emit, #299) ──
    Each PR's ``requested_reviewers`` list is checked against ``me``.
    Tracked via ``pr_review_requests`` — ``{pr_key: attempt_count}`` —
    where ``attempt_count`` is how many review turns actually started for
    this PR while ``me`` stayed requested.

    While ``me`` is a requested reviewer, recovery state distinguishes a
    queued/running turn from a failed turn. A queued/running event is left
    alone; a failed event consumes one attempt and is re-emitted, up to
    ``REVIEW_REQUEST_MAX_ATTEMPTS``. A submitted review removes ``me`` from
    ``requested_reviewers`` (GitHub clears it).

    On exhaustion (``attempt_count`` reaches the cap and ``me`` is STILL
    requested) it emits a one-shot ``pr_review_request_gave_up`` SIGNAL
    (negative algedonic, no turn) and parks the key at a dormant sentinel
    (``cap + 1``) so it neither retries nor re-gives-up. When ``me`` is
    removed (review submitted, PR closed, operator un-requests) the key
    drops out of the rebuilt dict, so a later re-request starts fresh at
    attempt 1.

    Empty ``me`` (no agent login configured) → review-request
    detection is silently skipped; push detection still runs.
    """
    # ``per_page=100`` (vs GitHub's 30 default) gives ~3× headroom against
    # the active-prune pitfall: a repo with >page-size open PRs would
    # silently drop everything past the first page from the cursor every
    # poll, so those PRs would re-record as "first sighting" each time and
    # never emit a synchronize event. Proper Link-header pagination is the
    # complete fix; per_page=100 is the cheap headroom bump until then.
    data = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=100",
        token,
    )
    new_heads: dict[str, str] = {}
    prior_review_requests: dict[str, int] = pr_review_requests or {}
    new_review_requests: dict[str, int] = {}
    trust_cache = trust_cache if trust_cache is not None else {}
    surfaced_untrusted = surfaced_untrusted if surfaced_untrusted is not None else set()
    review_needed_pr_numbers = (
        review_needed_pr_numbers if review_needed_pr_numbers is not None else set()
    )
    review_context = review_context if review_context is not None else {}
    current_open: set[str] = set()
    if not isinstance(data, list):
        # On API failure, preserve prior cursors so we don't false-fire
        # on the next successful poll. (If the poll truly missed a
        # push or review-request, we'll catch it next time.) Preserving
        # the attempt counts also means a transient poll failure doesn't
        # reset a PR's retry budget.
        return 0, dict(pr_heads), dict(prior_review_requests)
    count = 0
    for pr in data:
        # Push-detection self-filter: skip PRs the agent authored.
        # NOTE: this filter does NOT apply to review-request detection
        # below — the agent CAN be added as a reviewer to a PR it
        # authored (rare, but legal) and we'd want to surface that.
        pr_author = pr.get("user", {}).get("login")
        number = pr.get("number")
        if not number:
            continue
        key = str(number)
        current_open.add(key)

        title = pr.get("title", "")
        url = pr.get("html_url", "")
        explicitly_requested = _review_requested(pr, me)
        trusted_author = _pr_author_is_trusted(
            repo, number, url, token, trust_cache, tick_budget=tick_budget,
        )
        if trusted_author is None and explicitly_requested:
            # An explicit request is actionable regardless of author trust. Keep
            # the unresolved author fail-closed for automatic push review while
            # still allowing review-request reconciliation below.
            trusted_author = False
        if trusted_author is None:
            # Unresolved for lack of budget or a transport failure. Carry the
            # prior head forward — a dropped key would make the next tick treat
            # this PR as first-seen
            # and miss the push entirely.
            #
            # Deliberately does NOT hold the watermark: this pass takes no
            # ``since`` and defers entirely through ``pr_heads``, so the skipped
            # PR is compared again next tick regardless. Holding it here froze
            # `last_checked` on an ordinary tick over a single skipped PR, which
            # would stop the since-window advancing at all.
            if key in pr_heads:
                new_heads[key] = pr_heads[key]
            if tick_budget is not None:
                tick_budget.note_truncation("pushes_trust", 1)
            continue
        if not trusted_author:
            if not explicitly_requested and (not me or pr_author != me):
                count += _surface_untrusted_pr_once(
                    repo, number, url, surfaced_untrusted,
                )

        # ─── pr_synchronize (push detection) ───
        current_sha = (pr.get("head") or {}).get("sha")
        if current_sha and (not me or pr_author != me):
            prev_sha = pr_heads.get(key)
            if prev_sha is None:
                # First sighting — record, do not emit.
                new_heads[key] = current_sha
            elif prev_sha != current_sha:
                if not trusted_author:
                    new_heads[key] = current_sha
                else:
                    emitted = _emit_pr_synchronize(
                        repo,
                        number,
                        title,
                        url,
                        prev_sha,
                        current_sha,
                        token,
                        me,
                        pr=pr,
                        related_comment=review_context.get(key, ""),
                    )
                    if emitted:
                        count += 1
                        review_needed_pr_numbers.add(key)
                    new_heads[key] = current_sha
            else:
                new_heads[key] = current_sha

        # ─── pr_review_requested (reviewer added) ───
        # Skip if no agent login configured — nothing to match against.
        if me:
            currently_requested = explicitly_requested
            if currently_requested:
                # State reconciliation (chainlink #299): ``me`` being a
                # requested reviewer is usually the authoritative "review
                # still pending" signal. Exception (chainlink #669): an
                # operator can re-request the reviewer after a completed
                # review at the current head; in that case the request is
                # already satisfied, so drop it from the retry cursor.
                prior_review = None
                if current_sha:
                    prior_review = _latest_current_head_review(
                        repo, number, current_sha, me, token,
                    )
                    if prior_review:
                        requested_at = _latest_review_request_at(
                            repo, number, me, token,
                        )
                        if not _activity_postdates_review(
                            requested_at, prior_review,
                        ):
                            continue

                # Recovery state separates turns that failed from turns still
                # queued/running. Only real failures spend retry budget.
                prior_attempts = prior_review_requests.get(key, 0)
                if key in review_needed_pr_numbers:
                    # A synchronize/open event for this same PR already owns the
                    # review turn in this poll. Count that canonical turn against
                    # the request's retry budget so a failed review still retries
                    # next poll without gaining an extra hidden attempt.
                    if prior_attempts > REVIEW_REQUEST_MAX_ATTEMPTS:
                        new_review_requests[key] = prior_attempts
                    else:
                        new_review_requests[key] = min(
                            prior_attempts + 1,
                            REVIEW_REQUEST_MAX_ATTEMPTS,
                        )
                else:
                    recovery = _review_recovery_state(repo, key)
                    recovery_available = recovery is not None
                    if recovery is not None:
                        (
                            started, pending, found, canonical, _, _, _, _,
                        ) = recovery
                        if found:
                            prior_attempts = max(
                                started,
                                prior_attempts if canonical else 0,
                            )
                        if pending:
                            new_review_requests[key] = prior_attempts
                            continue

                    if prior_attempts < REVIEW_REQUEST_MAX_ATTEMPTS:
                        attempt = prior_attempts + 1
                        if attempt == 1:
                            status_line = (
                                f"You (@{me}) were added to the reviewers list."
                            )
                        else:
                            status_line = (
                                f"You (@{me}) are STILL on the reviewers list "
                                f"(re-request {attempt}/{REVIEW_REQUEST_MAX_ATTEMPTS}"
                                f" — a prior review request produced no submitted "
                                f"review; the turn may have failed). Submit the "
                                f"review this time."
                            )
                        if prior_review:
                            prior_state = str(
                                prior_review.get("state") or "substantive"
                            ).upper()
                            prior_submitted_at = str(
                                prior_review.get("submitted_at") or "unknown time"
                            )
                            status_line += (
                                f" The head is unchanged since your prior "
                                f"{prior_state} review submitted at "
                                f"{prior_submitted_at}; re-evaluate that review "
                                f"and the author's response rather than treating "
                                f"this as a new code revision."
                            )
                        prompt = (
                            f"Review requested on {repo} PR #{number}: "
                            f"{title} (by @{pr_author or 'unknown'})\n"
                            f"{status_line}\n"
                            f"{url}"
                        )
                        emitted = _emit_pr_review_needed(
                            prompt,
                            token=token,
                            reviewer=me,
                            current_head_reviewed=False,
                            event_type="pr_review_requested",
                            repo=repo,
                            number=number,
                            url=url,
                            requested_reviewer=me,
                            author=pr_author,
                            attempt=attempt,
                            max_attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
                            head_sha=current_sha,
                            head_repo=(pr.get("head") or {}).get("repo", {}).get("full_name"),
                            head_remote="source",
                            head_ref=(pr.get("head") or {}).get("ref"),
                            base_ref=(pr.get("base") or {}).get("ref"),
                            base_sha=(pr.get("base") or {}).get("sha"),
                            related_comment=review_context.get(key, ""),
                        )
                        if not emitted:
                            continue
                        count += 1
                        review_needed_pr_numbers.add(key)
                        # The framework stashes this event after the subprocess
                        # exits. Until recovery records an outcome, it has not spent
                        # an attempt. Direct script runs retain the legacy counter.
                        new_review_requests[key] = (
                            prior_attempts if recovery_available else attempt
                        )
                    elif prior_attempts == REVIEW_REQUEST_MAX_ATTEMPTS:
                        # Wedge guard exhausted: emitted the request
                        # REVIEW_REQUEST_MAX_ATTEMPTS times and ``me`` is still
                        # requested. Emit a one-shot give-up SIGNAL (no turn —
                        # re-spawning would just burn another likely-failing
                        # turn) so it surfaces in the negative algedonic block,
                        # then park at the dormant sentinel (cap + 1).
                        _emit_signal(
                            "pr_review_request_gave_up",
                            repo=repo,
                            number=number,
                            url=url,
                            requested_reviewer=me,
                            attempts=prior_attempts,
                        )
                        count += 1
                        new_review_requests[key] = prior_attempts + 1
                    else:
                        # Already gave up (sentinel > cap) and ``me`` is still
                        # requested. Stay dormant — carry the sentinel so we
                        # neither retry nor re-emit the give-up. Resets when
                        # ``me`` is removed (key drops from the rebuilt dict).
                        new_review_requests[key] = prior_attempts
    # Keep a notification marker for the PR's entire open lifetime. A
    # transient lookup recovery followed by another failure must not alert
    # again; closed PRs naturally prune from the rebuilt open-PR snapshot.
    surfaced_untrusted.intersection_update(current_open)
    return count, new_heads, new_review_requests


def _has_current_head_review(
    repo: str,
    number: int,
    head_sha: str,
    reviewer: str,
    token: str,
    *,
    activity_at: object = None,
) -> bool:
    """Return whether a current-head review still satisfies this activity.

    GitHub normally clears a reviewer from ``requested_reviewers`` when they
    submit a review, but an operator can re-request the same reviewer after a
    completed current-head review. Treat APPROVED, CHANGES_REQUESTED, and
    COMMENTED as substantive submitted reviews so the review-request retry
    loop does not page on an already-completed review. Activity after the latest
    such review is new information and no longer counts as satisfied.
    """
    review = _latest_current_head_review(
        repo, number, head_sha, reviewer, token,
    )
    if not review:
        return False
    return not _activity_postdates_review(activity_at, review)


def _latest_current_head_review(
    repo: str,
    number: int,
    head_sha: str,
    reviewer: str,
    token: str,
) -> dict | None:
    """Return the reviewer's latest substantive review at ``head_sha``."""
    if not head_sha or not reviewer:
        return None
    data = _gh_api(f"repos/{repo}/pulls/{number}/reviews", token)
    if not isinstance(data, list):
        return None
    substantive = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    matching: list[dict] = []
    for review in data:
        if not isinstance(review, dict):
            continue
        login = (review.get("user") or {}).get("login")
        state = str(review.get("state") or "").upper()
        commit_id = review.get("commit_id")
        if login == reviewer and commit_id == head_sha and state in substantive:
            matching.append(review)
    if not matching:
        return None
    return max(
        matching,
        key=lambda review: _parse_utc_datetime(
            str(review.get("submitted_at") or "")
        ) or datetime.min.replace(tzinfo=timezone.utc),
    )


def _latest_review_request_at(
    repo: str,
    number: int,
    reviewer: str,
    token: str,
) -> str | None:
    """Return the latest timeline request timestamp for ``reviewer``."""
    data = _gh_api(
        f"repos/{repo}/issues/{number}/timeline?per_page=100", token,
    )
    if not isinstance(data, list):
        return None
    timestamps = [
        str(event.get("created_at") or "")
        for event in data
        if isinstance(event, dict)
        and event.get("event") == "review_requested"
        and (event.get("requested_reviewer") or {}).get("login") == reviewer
        and _parse_utc_datetime(str(event.get("created_at") or "")) is not None
    ]
    if not timestamps:
        return None
    return max(timestamps, key=lambda value: _parse_utc_datetime(value))


def _activity_postdates_review(activity_at: object, review: dict) -> bool:
    """Return whether a request or comment is newer than ``review``."""
    if not isinstance(activity_at, str):
        return False
    activity_time = _parse_utc_datetime(activity_at)
    review_time = _parse_utc_datetime(str(review.get("submitted_at") or ""))
    return bool(activity_time and review_time and activity_time > review_time)


def _emit_pr_review_needed(
    prompt: str,
    *,
    token: str,
    reviewer: str,
    current_head_reviewed: bool | None = None,
    activity_at: object = None,
    **extras: object,
) -> bool:
    """Emit a PR work event unless ``reviewer`` reviewed its current head.

    All passes that can start a PR review turn flow through this choke point.
    Passes with a PR snapshot supply ``head_sha``; comment passes resolve the
    live PR so an old comment cannot make an already-reviewed head actionable.
    A comment after the latest current-head review is new information and may
    start another turn. API failures fail open, preserving first-request
    recovery semantics; missing or invalid activity timestamps fail closed once
    a current-head review is known.
    """
    reviewed = current_head_reviewed
    if reviewed is None and reviewer:
        repo = extras.get("repo")
        number = extras.get("number")
        head_sha = extras.get("head_sha") or extras.get("new_head")
        if not head_sha and isinstance(repo, str) and number is not None:
            pr = _gh_api(f"repos/{repo}/pulls/{number}", token)
            if isinstance(pr, dict):
                head_sha = (pr.get("head") or {}).get("sha")
        reviewed = bool(
            isinstance(repo, str)
            and number is not None
            and isinstance(head_sha, str)
            and _has_current_head_review(
                repo,
                number,
                head_sha,
                reviewer,
                token,
                activity_at=activity_at,
            )
        )
    if reviewed:
        return False
    _emit(prompt, **extras)
    return True


def _head_commit_date(repo: str, sha: str, token: str) -> str:
    """Committer date of ``sha`` (ISO-8601), or ``""`` when the lookup
    fails. Used by the changes-requested reconciliation to decide
    whether commits landed after the blocking review."""
    data = _gh_api(f"repos/{repo}/commits/{sha}", token)
    if not isinstance(data, dict):
        return ""
    commit = data.get("commit") or {}
    committer = commit.get("committer") or {}
    return str(committer.get("date") or "")


def _parse_utc_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize its instant to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compare_data(
    repo: str,
    base_sha: str,
    head_sha: str,
    token: str,
) -> dict | None:
    """GitHub compare payload for ``base_sha...head_sha``.

    Kept as a tiny wrapper so the CHANGES_REQUESTED reconciliation can
    name which compare shape it is using.  GitHub's ``files`` for a
    three-dot compare are relative to the merge base, not directly to
    ``base_sha``; callers must not treat a non-empty list here as
    "content changed since base_sha" without considering that shape.
    """
    if not base_sha or not head_sha:
        return None
    data = _gh_api(f"repos/{repo}/compare/{base_sha}...{head_sha}", token)
    return data if isinstance(data, dict) else None


def _compare_file_signature(data: dict) -> tuple[tuple[str, str, str, str], ...] | None:
    """Stable-enough signature of a compare payload's file patches.

    For normal text diffs, ``patch`` is the important part.  For binary
    or too-large diffs GitHub omits ``patch``; include the resulting blob
    SHA and counters so such files still participate in the equality
    check instead of being silently ignored.
    """
    files = data.get("files")
    if not isinstance(files, list):
        return None
    sig: list[tuple[str, str, str, str]] = []
    for file in files:
        if not isinstance(file, dict):
            return None
        filename = str(file.get("filename") or "")
        if not filename:
            return None
        previous = str(file.get("previous_filename") or "")
        status = str(file.get("status") or "")
        patch = file.get("patch")
        if patch is None:
            patch = "sha={sha};additions={additions};deletions={deletions};changes={changes}".format(
                sha=file.get("sha") or "",
                additions=file.get("additions") or 0,
                deletions=file.get("deletions") or 0,
                changes=file.get("changes") or 0,
            )
        sig.append((filename, previous, status, str(patch)))
    return tuple(sorted(sig))


def _head_changes_pr_diff_since_review(
    repo: str,
    review_sha: str,
    head_sha: str,
    current_base_sha: str,
    token: str,
) -> bool | None:
    """Return whether the current PR diff changed since the reviewed head.

    ``compare/{review_sha}...{head_sha}`` alone is not the answer: for a
    content-free rebase, GitHub reports a non-empty ``files`` list because
    the three-dot compare is from the merge base to the new head.  Instead:

    * If the reviewed commit is the merge base of the new head, commits
      really landed on top of the reviewed state, so suppress the stale
      reminder.
    * If the heads diverged, compare the PR patch signature at review
      time (old merge base → reviewed head) with the current PR patch
      signature (current base → current head).  Equal signatures mean the
      head only moved by rebase/freshening and the review is still stale.
    """
    if not review_sha or review_sha == head_sha:
        return False

    head_compare = _compare_data(repo, review_sha, head_sha, token)
    if head_compare is None:
        return None

    status = str(head_compare.get("status") or "").lower()
    merge_base = (head_compare.get("merge_base_commit") or {}).get("sha") or ""
    ahead_by = head_compare.get("ahead_by")
    behind_by = head_compare.get("behind_by")

    if status == "identical" or ahead_by == 0:
        return False
    if merge_base == review_sha and behind_by == 0:
        return True

    # Diverged/rebased: compare old PR diff to current PR diff.
    old_base_sha = merge_base
    if not old_base_sha or not current_base_sha:
        return None
    reviewed_diff = _compare_data(repo, old_base_sha, review_sha, token)
    current_diff = _compare_data(repo, current_base_sha, head_sha, token)
    if reviewed_diff is None or current_diff is None:
        return None
    reviewed_sig = _compare_file_signature(reviewed_diff)
    current_sig = _compare_file_signature(current_diff)
    if reviewed_sig is None or current_sig is None:
        return None
    return reviewed_sig != current_sig


def _check_own_changes_requested(
    repo: str,
    token: str,
    me: str,
    prior: dict[str, object],
    *,
    now: datetime | None = None,
    reminder_interval: timedelta = CHANGES_REQUESTED_REMINDER_INTERVAL,
    tick_budget: TickBudget | None = None,
    rotate_offset: int = 0,
) -> tuple[int, dict[str, object]]:
    """State-reconciling reminder for the agent's OWN open PRs stuck at
    CHANGES_REQUESTED (chainlink #449).

    Reviews ON the agent's PRs are otherwise edge-triggered only
    (``_check_pr_reviews`` emits each review once, then the cursor
    consumes it). A turn that triages the review without pushing fixes
    loses the work signal permanently — observed 2026-06-11: a batched
    turn read two request-changes reviews, merged a sibling approved
    PR, and ended; nothing ever re-fired the rework. This is the
    reverse-direction analog of the ``requested_reviewers``
    reconciliation above: the PR *state* (open, authored by ``me``,
    latest review per reviewer == CHANGES_REQUESTED, no commits since
    that review) is the authoritative "fixes still owed" signal, so it
    is re-derived from the live snapshot each poll.

    Reminder contract: ``prior`` maps each PR key to ``head_sha``,
    ``last_reminded_at``, and ``attempts``. As in the review-request path,
    an unchanged unresolved head re-emits up to
    ``REVIEW_REQUEST_MAX_ATTEMPTS``, then emits a one-shot ``*_gave_up``
    signal and parks at ``cap + 1``. Because emissions can remain queued
    without a delivered turn, the parked state re-arms after a long backstop;
    this preserves a bounded series without making queue loss permanently
    silence an unresolved PR. A head change starts a fresh attempt budget.
    Legacy entries migrate quietly as one prior attempt with
    ``last_reminded_at=now`` so deployment does not produce a reminder storm.
    Cleanup remains rebuild-on-every-poll: closed, merged, fixed, and
    no-longer-blocked PRs are not copied into the returned dict.

    "No commits since the review" matters: right after the agent
    pushes fixes the PR's review decision STAYS CHANGES_REQUESTED
    until a re-review lands, so reminding on that state would nag the
    agent for work it already did. A head commit newer than the latest
    blocking review → not stale, nothing emitted, nothing recorded
    (the state is re-evaluated next poll; if a reviewer then requests
    changes again, that review is newer than the head and fires).

    Empty ``me`` → skipped entirely (no self identity to match).
    On the PR-list API failing, ``prior`` is preserved unchanged so a
    transient failure doesn't re-fire already-reminded states.
    """
    if not me:
        return 0, {}
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    observed_at_iso = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=100",
        token,
    )
    if not isinstance(data, list):
        return 0, dict(prior)
    count = 0
    new: dict[str, object] = {}
    # Reconcile from where the previous tick stopped, so a truncated tick does not
    # starve the same tail every time (#1433).
    data = _rotate(list(data), rotate_offset)
    reconciled = 0
    for pr in data:
        if (pr.get("user") or {}).get("login") != me:
            continue
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha") or ""
        base_sha = (pr.get("base") or {}).get("sha") or ""
        if not number or not head_sha:
            continue
        key = str(number)
        # Out of budget: carry the prior entry forward untouched rather than
        # dropping it. `new` REPLACES the cursor entry, so omitting a key would
        # reset that PR's last_reminded_at and attempts — re-arming a reminder the
        # debounce exists to suppress and rewinding the give-up budget.
        if _truncate_here(tick_budget, reconciled, "changes_requested"):
            if key in prior:
                new[key] = prior[key]
            continue
        reconciled += 1
        reviews = _gh_api(f"repos/{repo}/pulls/{number}/reviews", token)
        if not isinstance(reviews, list):
            # Cannot determine review state — preserve the dedupe entry
            # so a transient failure doesn't cause a duplicate reminder.
            if key in prior:
                new[key] = prior[key]
            continue
        # Latest substantive review per reviewer. COMMENTED/PENDING/
        # DISMISSED don't change the blocking state; a reviewer's later
        # APPROVED clears their earlier CHANGES_REQUESTED.
        latest: dict[str, tuple[str, str, str]] = {}
        for review in reviews:
            login = (review.get("user") or {}).get("login") or ""
            state = (review.get("state") or "").upper()
            submitted = review.get("submitted_at") or ""
            commit_id = review.get("commit_id") or ""
            if not login or login == me or not submitted:
                continue
            if state not in ("APPROVED", "CHANGES_REQUESTED"):
                continue
            cur = latest.get(login)
            if cur is None or submitted > cur[0]:
                latest[login] = (submitted, state, commit_id)
        blocking = {
            login: (ts, commit_id)
            for login, (ts, st, commit_id) in latest.items()
            if st == "CHANGES_REQUESTED"
        }
        if not blocking:
            continue  # not blocked — entry drops; a later CR cycle starts fresh
        # Stale only when no commits landed after the newest blocking
        # review. An unknown head-commit date (API hiccup) counts as
        # stale; the elapsed-time floor bounds reminders during hiccups.
        newest_block_ts, newest_block_sha = max(
            blocking.values(), key=lambda item: item[0],
        )
        head_date = _head_commit_date(repo, head_sha, token)
        head_datetime = _parse_utc_datetime(head_date)
        review_datetime = _parse_utc_datetime(newest_block_ts)
        if (
            head_datetime is not None
            and review_datetime is not None
            and head_datetime >= review_datetime
        ):
            changed = _head_changes_pr_diff_since_review(
                repo, newest_block_sha, head_sha, base_sha, token,
            )
            if changed is not False:
                continue

        prior_entry = prior.get(key)
        if isinstance(prior_entry, str):
            # Pre-cadence cursor. Treat its prior one-shot reminder as attempt
            # one now so migration cannot fan out across every tracked PR.
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": observed_at_iso,
                "attempts": 1,
            }
            continue

        last_reminded_at = ""
        last_reminded: datetime | None = None
        prior_attempts = 0
        attempt_reasons: list[str] = []
        recovery_after = ""
        head_changed = False
        if isinstance(prior_entry, dict):
            prior_head = prior_entry.get("head_sha")
            raw_attempts = prior_entry.get("attempts")
            if "attempts" not in prior_entry:
                # The previous structured cursor is also legacy. Quietly
                # migrate it instead of interpreting every entry as attempt 0.
                new[key] = {
                    "head_sha": head_sha,
                    "last_reminded_at": observed_at_iso,
                    "attempts": 1,
                }
                continue
            if (
                not isinstance(raw_attempts, int)
                or isinstance(raw_attempts, bool)
                or raw_attempts < 0
            ):
                # Quiet repair for malformed structured state.
                new[key] = {
                    "head_sha": head_sha,
                    "last_reminded_at": observed_at_iso,
                    "attempts": 1,
                }
                continue
            if prior_head != head_sha:
                # A push is the observable outcome of the prior remediation.
                # If a newer review still leaves this head stale, it is a new
                # bounded attempt series rather than continuation of the old.
                head_changed = True
            else:
                prior_attempts = raw_attempts
                raw_after = prior_entry.get("rearmed_at")
                if isinstance(raw_after, str):
                    recovery_after = raw_after
            value = prior_entry.get("last_reminded_at")
            if not head_changed and isinstance(value, str):
                last_reminded_at = value
                try:
                    last_reminded = datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
                    if last_reminded.tzinfo is None:
                        last_reminded = last_reminded.replace(tzinfo=timezone.utc)
                    last_reminded = last_reminded.astimezone(timezone.utc)
                except ValueError:
                    last_reminded = None

        recovery = _review_recovery_state(
            repo,
            key,
            event_types=_CHANGES_REQUESTED_TURN_EVENT_TYPES,
            after=recovery_after,
            head_sha=head_sha,
            pending_before=(
                observed_at - CHANGES_REQUESTED_GAVE_UP_BACKSTOP
            ).isoformat(),
        )
        recovery_available = recovery is not None
        latest_refusal_at = ""
        refusal_reasons: list[str] = []
        self_clearing_refusals = 0
        refusal_classification = ""
        if recovery is not None:
            (
                charged, pending, found, _, attempt_reasons,
                latest_refusal_at, refusal_reasons, self_clearing_refusals,
            ) = recovery
            if found and prior_attempts <= REVIEW_REQUEST_MAX_ATTEMPTS:
                # Recovery is authoritative over legacy emission-count cursors.
                prior_attempts = charged
            if pending:
                new[key] = {
                    "head_sha": head_sha,
                    "last_reminded_at": last_reminded_at or observed_at_iso,
                    "attempts": prior_attempts,
                    **({"rearmed_at": recovery_after} if recovery_after else {}),
                }
                continue

        if latest_refusal_at:
            refused_at = _parse_utc_datetime(latest_refusal_at)
            refusal_classification = _classify_refusal_reasons(refusal_reasons)
            refusal_series_gave_up = (
                refusal_classification == _REFUSAL_SELF_CLEARING
                and self_clearing_refusals >= REVIEW_REQUEST_MAX_ATTEMPTS
            )
            suppress_until_backstop = (
                refusal_classification == _REFUSAL_OPERATOR_GATED
                or refusal_series_gave_up
            )
            if refused_at is not None and suppress_until_backstop and (
                observed_at - refused_at < CHANGES_REQUESTED_GAVE_UP_BACKSTOP
            ):
                # Permanent faults stay quiet. Self-clearing refusals get the
                # normal cadence, but a repeatedly refused series is bounded too.
                new[key] = {
                    "head_sha": head_sha,
                    "last_reminded_at": last_reminded_at or observed_at_iso,
                    "attempts": prior_attempts,
                    **({"rearmed_at": recovery_after} if recovery_after else {}),
                }
                continue
            if refused_at is not None and refusal_series_gave_up:
                # Exclude the exhausted refusal series after its daily re-arm;
                # refusal outcomes remain uncharged from the attempt budget.
                recovery_after = observed_at_iso

        eligible_after = max(
            timedelta(0),
            reminder_interval - CHANGES_REQUESTED_REMINDER_SLACK,
        )
        if last_reminded is not None and observed_at - last_reminded < eligible_after:
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": last_reminded_at,
                "attempts": prior_attempts,
            }
            continue
        if key in prior and last_reminded is None and not head_changed:
            # Quietly repair malformed structured state just like a legacy
            # entry rather than turning cursor corruption into an emit storm.
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": observed_at_iso,
                "attempts": max(prior_attempts, 1),
            }
            continue

        title = pr.get("title", "")
        url = pr.get("html_url", "")
        if prior_attempts >= REVIEW_REQUEST_MAX_ATTEMPTS:
            if prior_attempts == REVIEW_REQUEST_MAX_ATTEMPTS:
                _emit_signal(
                    "pr_changes_requested_gave_up",
                    repo=repo,
                    number=number,
                    url=url,
                    head_sha=head_sha,
                    attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
                    attempt_reasons=attempt_reasons,
                )
                count += 1
                prior_attempts += 1
                last_reminded_at = observed_at_iso
                last_reminded = observed_at
            if (
                last_reminded is None
                or observed_at - last_reminded < CHANGES_REQUESTED_GAVE_UP_BACKSTOP
            ):
                new[key] = {
                    "head_sha": head_sha,
                    "last_reminded_at": last_reminded_at,
                    "attempts": prior_attempts,
                }
                continue
            # Emission is not delivery: a whole bounded series may have sat in
            # an in-memory queue without any turn running. Re-arm after a long
            # backstop so the cap limits noise but cannot silence the PR forever.
            prior_attempts = 0
            recovery_after = observed_at_iso if recovery_available else ""

        attempt = prior_attempts + 1
        reviewers = ", ".join(f"@{login}" for login in sorted(blocking))
        prompt = (
            f"Your PR #{number} on {repo} is stuck at CHANGES_REQUESTED: "
            f"{title}\n"
            f"{reviewers} requested changes and no commits have landed "
            f"since (head {head_sha[:8]}). Address the review feedback, "
            f"{_verification_guidance()}, push the fixes, and re-request "
            f"review.\n{url}"
        )
        _emit(
            prompt,
            event_type="pr_changes_requested_stale",
            repo=repo,
            number=number,
            url=url,
            head_sha=head_sha,
            head_repo=((pr.get("head") or {}).get("repo") or {}).get("full_name"),
            head_remote="origin",
            head_ref=(pr.get("head") or {}).get("ref"),
            base_ref=(pr.get("base") or {}).get("ref"),
            base_sha=base_sha,
            reviewers=sorted(blocking),
            author=(pr.get("user") or {}).get("login"),
            attempt=attempt,
            max_attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
            prior_refusal_reasons=refusal_reasons,
            prior_refusal_classification=(refusal_classification or None),
            prior_self_clearing_refusals=self_clearing_refusals,
        )
        count += 1
        new[key] = {
            "head_sha": head_sha,
            "last_reminded_at": observed_at_iso,
            "attempts": prior_attempts if recovery_available else attempt,
            **({"rearmed_at": recovery_after} if recovery_after else {}),
        }
    return count, new


def _blocking_reviewers(reviews: list, me: str) -> list[str]:
    """Return reviewers whose latest substantive review is blocking."""
    latest: dict[str, tuple[str, str]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        login = (review.get("user") or {}).get("login") or ""
        state = str(review.get("state") or "").upper()
        submitted = review.get("submitted_at") or ""
        if not login or login == me or not submitted:
            continue
        if state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        current = latest.get(login)
        if current is None or submitted > current[0]:
            latest[login] = (submitted, state)
    return sorted(
        login for login, (_submitted, state) in latest.items()
        if state == "CHANGES_REQUESTED"
    )


def _check_pr_ci_failures(
    repo: str,
    since: str,
    token: str,
    me: str,
    prior: dict[str, object],
    *,
    now: datetime | None = None,
    tick_budget: TickBudget | None = None,
    rotate_offset: int = 0,
) -> tuple[int, dict[str, object]]:
    """Route newly completed check failures for open PRs.

    Owned PRs receive a mutation-capable remediation event. Other authors only
    produce a notification signal. API failures preserve affected cursor entries.
    """
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observed_iso = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    prior_checked = prior.get("_last_checked")
    window_since = prior_checked if isinstance(prior_checked, str) else since
    since_dt = _parse_utc_datetime(window_since)
    prs = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=100",
        token,
    )
    if not isinstance(prs, list):
        preserved = dict(prior)
        preserved["_last_checked"] = window_since
        return 0, preserved

    count = 0
    new: dict[str, object] = {}
    collection_complete = True
    reconciled = 0
    prs = _rotate(list(prs), rotate_offset)
    for listed in prs:
        number = listed.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        key = str(number)
        if _truncate_here(tick_budget, reconciled, "ci_failures"):
            # An incomplete collection holds `_last_checked` at window_since, so
            # a truncated PR's checks are re-examined next tick rather than lost.
            collection_complete = False
            if key in prior:
                new[key] = prior[key]
            continue
        reconciled += 1
        pr = _gh_api(f"repos/{repo}/pulls/{number}", token)
        if not isinstance(pr, dict):
            collection_complete = False
            if key in prior:
                new[key] = prior[key]
            continue
        if pr.get("state") != "open" or pr.get("merged") is True or pr.get("merged_at"):
            continue
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        head_sha = head.get("sha") or ""
        if not head_sha:
            collection_complete = False
            if key in prior:
                new[key] = prior[key]
            continue
        checks_data = _gh_api(
            f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100", token,
        )
        checks = checks_data.get("check_runs") if isinstance(checks_data, dict) else None
        total_checks = checks_data.get("total_count") if isinstance(checks_data, dict) else None
        if (
            not isinstance(checks, list)
            or isinstance(total_checks, int) and total_checks > len(checks)
        ):
            collection_complete = False
            if key in prior:
                new[key] = prior[key]
            continue
        failures = [
            check for check in checks
            if isinstance(check, dict)
            and check.get("status") == "completed"
            and check.get("conclusion") in CI_FAILURE_CONCLUSIONS
        ]
        if not failures:
            continue

        delivery_key = _delivery_key(repo, number, head_sha, failures)
        entry = prior.get(key)
        same_failure = isinstance(entry, dict) and entry.get("delivery_key") == delivery_key
        raw_emitted_at = entry.get("emitted_at") if isinstance(entry, dict) else None
        emitted_at = (
            _parse_utc_datetime(raw_emitted_at)
            if isinstance(raw_emitted_at, str) else None
        )
        delivered = _delivery_receipt_exists(delivery_key)
        if same_failure and (
            entry.get("baseline") is True
            or delivered
            or emitted_at is not None
            and observed_at - emitted_at < CI_DELIVERY_RETRY_INTERVAL
        ):
            new[key] = entry
            continue

        newly_completed = any(
            isinstance(check.get("completed_at"), str)
            and (completed := _parse_utc_datetime(check["completed_at"])) is not None
            and (since_dt is None or completed > since_dt)
            for check in failures
        )
        if not newly_completed and not (same_failure and not delivered):
            new[key] = {
                "head_sha": head_sha,
                "delivery_key": delivery_key,
                "emitted_at": observed_iso,
                "baseline": True,
            }
            continue

        if not _claim_delivery(delivery_key, observed_at):
            new[key] = {
                "head_sha": head_sha,
                "delivery_key": delivery_key,
                "emitted_at": observed_iso,
            }
            continue

        failed_checks = [
            {
                "id": check.get("id"),
                "name": check.get("name") or "unknown",
                "conclusion": check.get("conclusion"),
                "url": check.get("html_url") or check.get("details_url") or "",
                "details_url": check.get("details_url") or "",
                "external_id": check.get("external_id"),
            }
            for check in failures
        ]
        names = ", ".join(
            f"{check['name']} ({check['conclusion']})" for check in failed_checks
        )
        author = (pr.get("user") or {}).get("login") or ""
        common = dict(
            repo=repo,
            number=number,
            url=pr.get("html_url", ""),
            head_sha=head_sha,
            author=author,
            failed_checks=failed_checks,
            delivery_key=delivery_key,
        )
        if me and author == me:
            prompt = (
                f"CI failed on your open PR #{number} on {repo} at immutable head "
                f"{head_sha}: {names}. Re-check the live PR, exact head, and current "
                "checks before changing anything. If it is still open at this head and "
                "still red, inspect the linked check logs, fix the failure, run the "
                f"repository's configured tests, and push with a lease.\n{pr.get('html_url', '')}"
            )
            _emit(
                prompt,
                event_type="pr_ci_failure",
                head_repo=((head.get("repo") or {}).get("full_name")),
                head_remote="origin",
                head_ref=head.get("ref"),
                base_ref=base.get("ref"),
                base_sha=base.get("sha"),
                **common,
            )
        else:
            _emit_signal("pr_ci_failure_external", **common)
        count += 1
        new[key] = {
            "head_sha": head_sha,
            "delivery_key": delivery_key,
            "emitted_at": observed_iso,
        }
    for key, old_entry in prior.items():
        if not isinstance(old_entry, dict):
            continue
        current_entry = new.get(key)
        if (
            not isinstance(current_entry, dict)
            or current_entry.get("delivery_key") != old_entry.get("delivery_key")
        ):
            _remove_delivery_artifacts(old_entry.get("delivery_key"))
    new["_last_checked"] = observed_iso if collection_complete else window_since
    return count, new


def _check_own_mergeability(
    repo: str,
    token: str,
    me: str,
    prior: dict[str, object],
    *,
    now: datetime | None = None,
    attempt_budget: list[int] | None = None,
    retry_interval: timedelta = MERGEABILITY_RETRY_INTERVAL,
    tick_budget: TickBudget | None = None,
    rotate_offset: int = 0,
) -> tuple[int, dict[str, object]]:
    """Reconcile non-review merge failures on the agent's own open PRs.

    Only a computed boolean ``mergeable`` is actionable. Attempts are bounded
    per (PR, head, reason), and ``attempt_budget`` limits the whole poll cycle.
    """
    if not me:
        return 0, {}
    budget = attempt_budget if attempt_budget is not None else [1]
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    observed_iso = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    prs = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc&per_page=100",
        token,
    )
    if not isinstance(prs, list):
        return 0, dict(prior)

    count = 0
    new: dict[str, object] = {}
    reconciled = 0
    prs = _rotate(list(prs), rotate_offset)
    for listed_pr in prs:
        if (listed_pr.get("user") or {}).get("login") != me:
            continue
        number = listed_pr.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        key = str(number)
        if _truncate_here(tick_budget, reconciled, "mergeability"):
            if key in prior:
                new[key] = prior[key]
            continue
        reconciled += 1
        pr = _gh_api(f"repos/{repo}/pulls/{number}", token)
        if not isinstance(pr, dict):
            if key in prior:
                new[key] = prior[key]
            continue
        # Listing and detail retrieval are not atomic. Only the live detail
        # response may authorize work if the PR merges between those requests.
        if pr.get("state") != "open" or pr.get("merged") is True or pr.get("merged_at"):
            continue
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        head_sha = head.get("sha") or ""
        base_sha = base.get("sha") or ""
        mergeable = pr.get("mergeable")
        if not head_sha or not base_sha or not isinstance(mergeable, bool):
            if key in prior:
                new[key] = prior[key]
            continue
        compare = _compare_data(repo, base_sha, head_sha, token)
        if compare is None:
            if key in prior:
                new[key] = prior[key]
            continue
        behind_by = compare.get("behind_by")
        if not isinstance(behind_by, int) or isinstance(behind_by, bool):
            if key in prior:
                new[key] = prior[key]
            continue
        if mergeable and behind_by <= 0:
            continue

        reviews = _gh_api(f"repos/{repo}/pulls/{number}/reviews", token)
        if not isinstance(reviews, list):
            if key in prior:
                new[key] = prior[key]
            continue
        blocking = _blocking_reviewers(reviews, me)
        if blocking:
            # Moving the head could make an unaddressed blocking review appear
            # stale. The changes-requested reconciler owns this PR until cleared.
            continue

        reason = "conflicting" if not mergeable else "behind_base"
        attempts = 0
        last_attempt: datetime | None = None
        entry = prior.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("head_sha") == head_sha
            and entry.get("reason") == reason
        ):
            raw_attempts = entry.get("attempts")
            if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool):
                attempts = max(raw_attempts, 0)
            raw_last = entry.get("last_attempt_at")
            if isinstance(raw_last, str):
                last_attempt = _parse_utc_datetime(raw_last)
        if last_attempt is not None and observed_at - last_attempt < retry_interval:
            new[key] = entry
            continue
        if attempts > REVIEW_REQUEST_MAX_ATTEMPTS:
            new[key] = entry
            continue
        if not budget or budget[0] <= 0:
            if entry is not None:
                new[key] = entry
            continue
        if attempts == REVIEW_REQUEST_MAX_ATTEMPTS:
            _emit_signal(
                "pr_mergeability_rebase_gave_up",
                repo=repo,
                number=number,
                url=pr.get("html_url", ""),
                head_sha=head_sha,
                base_sha=base_sha,
                reason=reason,
                attempts=attempts,
            )
            new[key] = {
                "head_sha": head_sha,
                "reason": reason,
                "last_attempt_at": observed_iso,
                "attempts": attempts + 1,
            }
            budget[0] -= 1
            count += 1
            continue

        attempt = attempts + 1
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        common = dict(
            repo=repo,
            number=number,
            url=url,
            head_sha=head_sha,
            head_repo=((head.get("repo") or {}).get("full_name")),
            head_remote="origin",
            head_ref=head.get("ref"),
            base_ref=base.get("ref"),
            base_sha=base_sha,
            author=(pr.get("user") or {}).get("login"),
            attempt=attempt,
            max_attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
        )
        if mergeable:
            prompt = (
                f"Your PR #{number} on {repo} is {behind_by} commit(s) behind its "
                f"declared base {base.get('ref')} at {base_sha}: {title}\n"
                "It has no blocking CHANGES_REQUESTED review and GitHub reports the "
                "merge as clean. Use the scoped PR checkout, rebase onto the lease's "
                "declared base commit, run the repository tests, then repo_push. The "
                f"push must retain its lease against starting head {head_sha}; if the "
                "lease is stale, do not retry or overwrite the concurrent push. Do not "
                f"merge the PR.\n{url}"
            )
            event_type = "pr_mergeability_rebase"
        else:
            reviewers = sorted({
                (review.get("user") or {}).get("login")
                for review in reviews
                if isinstance(review, dict)
                and (review.get("user") or {}).get("login")
                and (review.get("user") or {}).get("login") != me
            } | {
                reviewer.get("login")
                for reviewer in (pr.get("requested_reviewers") or [])
                if isinstance(reviewer, dict)
                and reviewer.get("login")
                and reviewer.get("login") != me
            })
            reviewer_text = ", ".join(reviewers) if reviewers else "an available reviewer"
            prompt = (
                f"Your PR #{number} on {repo} conflicts with its declared base "
                f"{base.get('ref')} at {base_sha}: {title}\n"
                "Use the scoped PR checkout and repo_rebase to reproduce the conflict, "
                "then collect every path with repo_unmerged. Resolve only when you can "
                "identify the intended property from both the base and head; prefer "
                "repo_rebase_abort and escalation over guessing. Stage only the conflict "
                "paths and call repo_rebase again with the base property, head property, "
                "and how you verified each so that evidence is recorded in the rebased "
                "commit. Run repo_test with no selectors for the repository's configured "
                "full suite; never push if it fails. Then repo_push with the existing lease "
                f"and re-request review from {reviewer_text}. This path only resolves and "
                "re-requests review: do not merge the PR.\n"
                f"There is no blocking CHANGES_REQUESTED review. Base: {base_sha}.\n{url}"
            )
            event_type = "pr_mergeability_conflicting"
        _emit(
            prompt,
            event_type=event_type,
            behind_by=behind_by,
            **({"reviewers": reviewers} if not mergeable else {}),
            **common,
        )
        new[key] = {
            "head_sha": head_sha,
            "reason": reason,
            "last_attempt_at": observed_iso,
            "attempts": attempt,
        }
        budget[0] -= 1
        count += 1
    return count, new


def _check_pr_reviews(
    repo: str, since: str, token: str, me: str,
    *, tick_budget: "TickBudget | None" = None,
) -> int:
    """New PR reviews (approve / changes-requested / commented).
    No ``since=`` query on reviews endpoint — walk open PRs + filter
    by ``submitted_at``. ``_gh_api`` passes ``--paginate``, so all
    open PRs are walked regardless of page size (verified
    empirically: ``per_page=3 --paginate`` returns every PR, not 3).
    Letting GitHub's default page size (30) apply means fewer
    round-trips on repos with many open PRs."""
    prs = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=updated&direction=desc",
        token,
    )
    if not isinstance(prs, list):
        _refused_window(tick_budget, prs, "pr_reviews_window")
        return 0
    count = 0
    for pr in prs:
        pr_number = pr.get("number")
        if not pr_number:
            continue
        if _hard_stop(tick_budget, "pr_reviews"):
            break
        reviews = _gh_api(f"repos/{repo}/pulls/{pr_number}/reviews", token)
        if not isinstance(reviews, list):
            if _refused_window(tick_budget, reviews, "pr_reviews_window"):
                break
            continue
        for review in reviews:
            if me and review.get("user", {}).get("login") == me:
                continue
            submitted = review.get("submitted_at", "") or ""
            if not submitted or submitted <= since:
                continue
            state = (review.get("state") or "").upper()
            if state == "PENDING":
                continue
            reviewer_login = review.get("user", {}).get("login", "unknown")
            body = _truncate(review.get("body") or "")
            url = review.get("html_url", "")
            pr_title = pr.get("title", "")
            state_label = {
                "APPROVED": "approved",
                "CHANGES_REQUESTED": "requested changes on",
                "COMMENTED": "reviewed",
                "DISMISSED": "dismissed review on",
            }.get(state, f"reviewed ({state})")
            prompt = (
                f"@{reviewer_login} {state_label} PR #{pr_number} "
                f"({pr_title}) on {repo}"
            )
            if body:
                prompt += f"\n{body}"
            prompt += f"\n{url}"
            # ``author`` carries the PR's author, not the reviewer, matching
            # the pr_review_requested pass. The framework reads it as the scope
            # principal: a CHANGES_REQUESTED review on a PR this agent authored
            # is what grants remediation authority (write/commit/push), and
            # naming the reviewer here left the agent able to read the review
            # but not to act on it.
            _emit(prompt, event_type="pr_review",
                  repo=repo, number=pr_number, url=url, state=state,
                  author=(pr.get("user") or {}).get("login"),
                  reviewer=reviewer_login,
                  **_pr_scope_fields(pr, repo))
            count += 1
    return count


# ─── main ─────────────────────────────────────────────────────────────


_STATE_GITIGNORE = """\
# Transient github-poller state — seeded by the github-poller skill
# (write-if-missing; edit freely). git reads per-directory .gitignore natively,
# so this keeps the high-churn cursor out of the home's tracked git history
# while the home allowlist still tracks anything durable.
cursor.json
*.tmp
.delivery-receipts/
.delivery-claims/
"""


def _seed_state_gitignore() -> None:
    """Seed STATE_DIR/.gitignore (only if absent) so the poller's transient
    cursor isn't committed to the home repo. Best-effort; never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gi = STATE_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text(_STATE_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    _seed_state_gitignore()
    repos_str = os.environ.get("GITHUB_REPOS", "").strip()
    if not repos_str:
        # Silent exit: poller is installed but operator hasn't configured
        # any repos. Don't emit, don't error — the framework treats
        # silence as "nothing to report."
        print("GITHUB_REPOS not set; nothing to do", file=sys.stderr)
        return

    repos = [r.strip() for r in repos_str.split(",") if r.strip()]
    if not repos:
        return

    token = _resolve_token()
    if not token:
        print(
            "No GitHub token (set GITHUB_TOKEN or authenticate gh CLI)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Self-filter: explicit env override only. Auto-detect via
    # ``gh api user`` is wrong when the PAT belongs to the operator
    # (filtering them out would silence the signal we want).
    me = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
    if me:
        print(f"Filtering events authored by @{me}", file=sys.stderr)
    else:
        print(
            "MIMIR_GITHUB_SELF_LOGIN unset — no self-filter active",
            file=sys.stderr,
        )

    cursor = _load_cursor()
    new_cursor_ts = _utc_now_iso()
    since = cursor.get("last_checked")
    if not since:
        since = (
            datetime.now(timezone.utc) - FIRST_RUN_LOOKBACK
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"First run; looking back to {since}", file=sys.stderr)

    pr_heads_all: dict[str, dict[str, str]] = cursor.get("pr_heads", {}) or {}
    new_pr_heads_all: dict[str, dict[str, str]] = {}
    # Review-request cursor (chainlink #299): ``{repo: {pr_key: attempts}}``
    # where ``attempts`` counts pr_review_requested emits while ``me`` stayed
    # a requested reviewer. _coerce_review_requests migrates the pre-#299
    # bare-list format (``{repo: [pr_key, ...]}``) on first load.
    rr_all: dict = cursor.get("pr_review_requests", {}) or {}
    new_rr_all: dict[str, dict[str, int]] = {}
    # Changes-requested reconciliation cursor (chainlink #449):
    # ``{repo: {pr_key: {head_sha, last_reminded_at, attempts}}}`` — bounded
    # unresolved own-PR remediation attempts, rate-limited by elapsed time.
    budget = TickBudget(started_at=_PROCESS_START)
    # Clamp every gh api call for the rest of this tick so no single slow request
    # can carry the subprocess past the framework's SIGKILL.
    set_active_tick_budget(budget)
    # Rotation offsets per repo, so a truncated tick resumes at the tail it skipped
    # instead of starving it every tick (#1433).
    reconcile_offsets: dict = cursor.get("pr_reconcile_offsets", {}) or {}
    new_reconcile_offsets: dict[str, int] = {}
    cr_all: dict = cursor.get("pr_changes_requested", {}) or {}
    new_cr_all: dict[str, dict[str, object]] = {}
    mergeability_all: dict = cursor.get("pr_mergeability", {}) or {}
    new_mergeability_all: dict[str, dict[str, object]] = {}
    ci_failures_all: dict = cursor.get("pr_ci_failures", {}) or {}
    new_ci_failures_all: dict[str, dict[str, object]] = {}
    mergeability_attempt_budget = [1]
    untrusted_all: dict = cursor.get("pr_untrusted_authors", {}) or {}
    new_untrusted_all: dict[str, list[str]] = {}
    trust_cache: dict[tuple[str, object], object] = {}

    total = 0
    for repo in repos:
        print(f"Checking {repo} since {since}...", file=sys.stderr)
        total += _check_issues(repo, since, token, me, tick_budget=budget)
        surfaced_untrusted = {
            str(value) for value in (untrusted_all.get(repo, []) or [])
            if isinstance(value, (str, int)) and not isinstance(value, bool)
        }
        review_needed_pr_numbers: set[str] = set()
        issue_comments, review_context = _collect_issue_comment_context(
            repo, since, token, me,
        )
        pr_opened_count = _check_prs(
            repo, since, token, me, trust_cache, surfaced_untrusted,
            review_needed_pr_numbers=review_needed_pr_numbers,
            review_context=review_context,
            tick_budget=budget,
        )
        total += pr_opened_count
        total += _check_pr_review_comments(
            repo, since, token, me, tick_budget=budget,
        )
        total += _check_pr_reviews(repo, since, token, me, tick_budget=budget)
        repo_heads = pr_heads_all.get(repo, {}) or {}
        repo_rr = _coerce_review_requests(rr_all.get(repo))
        push_count, new_repo_heads, new_repo_rr = _check_pr_pushes(
            repo, token, me, repo_heads, pr_review_requests=repo_rr,
            trust_cache=trust_cache,
            surfaced_untrusted=surfaced_untrusted,
            review_needed_pr_numbers=review_needed_pr_numbers,
            review_context=review_context,
            tick_budget=budget,
        )
        total += push_count
        total += _check_issue_comments(
            repo,
            since,
            token,
            me,
            review_needed_pr_numbers=review_needed_pr_numbers,
            comments=issue_comments,
            tick_budget=budget,
        )
        new_pr_heads_all[repo] = new_repo_heads
        new_rr_all[repo] = new_repo_rr
        new_untrusted_all[repo] = sorted(surfaced_untrusted)
        # One offset drives all three per-PR passes, so a tick concentrates its
        # budget on the same slice of PRs instead of three disjoint partial views.
        repo_offset = int(reconcile_offsets.get(repo, 0) or 0)
        truncated_before = sum(budget.truncated.values())
        repo_cr = cr_all.get(repo, {}) or {}
        cr_count, new_repo_cr = _check_own_changes_requested(
            repo, token, me, repo_cr,
            tick_budget=budget,
            rotate_offset=repo_offset,
        )
        total += cr_count
        new_cr_all[repo] = new_repo_cr
        repo_mergeability = mergeability_all.get(repo, {}) or {}
        mergeability_count, new_repo_mergeability = _check_own_mergeability(
            repo, token, me, repo_mergeability,
            attempt_budget=mergeability_attempt_budget,
            tick_budget=budget,
            rotate_offset=repo_offset,
        )
        total += mergeability_count
        new_mergeability_all[repo] = new_repo_mergeability
        repo_ci = ci_failures_all.get(repo, {}) or {}
        ci_count, new_repo_ci = _check_pr_ci_failures(
            repo, since, token, me, repo_ci,
            tick_budget=budget,
            rotate_offset=repo_offset,
        )
        total += ci_count
        new_ci_failures_all[repo] = new_repo_ci
        # Move the window only when this repo actually left PRs unreconciled; a
        # complete pass already covered every PR, so restart it at the head.
        if sum(budget.truncated.values()) > truncated_before:
            new_reconcile_offsets[repo] = repo_offset + PR_RECONCILE_MIN_PER_PASS
        else:
            new_reconcile_offsets[repo] = 0

    if budget.hard_truncated:
        # The hard deadline cut per-PR work short. Holding the watermark keeps
        # the since-based passes (notably _check_pr_reviews, which has no dedupe
        # cursor of its own) from stepping over events this tick never reached.
        # Re-delivering a nudge is recoverable; dropping a review is not, and
        # today's SIGKILL already leaves the watermark unadvanced.
        _emit_signal(
            "poller_tick_hard_deadline",
            elapsed_seconds=round(budget.elapsed(), 1),
            startup_consumed_seconds=round(budget.startup_consumed, 1),
            hard_deadline_seconds=TICK_HARD_DEADLINE_SECONDS,
            truncated=dict(budget.truncated),
        )
    else:
        cursor["last_checked"] = new_cursor_ts
    cursor["pr_heads"] = new_pr_heads_all
    cursor["pr_review_requests"] = new_rr_all
    cursor["pr_reconcile_offsets"] = new_reconcile_offsets or reconcile_offsets
    if budget.truncated:
        # Never truncate silently: a tick that skipped PRs must say so, or the
        # next reader mistakes partial coverage for a quiet repository.
        _emit_signal(
            "poller_pr_reconcile_truncated",
            truncated=dict(budget.truncated),
            deadline_seconds=PR_RECONCILE_DEADLINE_SECONDS,
        )
    cursor["pr_changes_requested"] = new_cr_all
    cursor["pr_mergeability"] = new_mergeability_all
    cursor["pr_ci_failures"] = new_ci_failures_all
    cursor["pr_untrusted_authors"] = new_untrusted_all
    set_active_tick_budget(None)
    _save_cursor(cursor)
    print(
        f"Emitted {total} event(s) across {len(repos)} repo(s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
