#!/usr/bin/env python3
"""GitHub repository poller — pollers.json contract (chainlink #3).

Checks each ``GITHUB_REPOS`` entry for new issues, PRs, conversation
comments, PR review comments (inline diff), and PR reviews since the
last cursor. Emits one JSONL event per actionable item to stdout.

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

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
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

# Unresolved review feedback is re-reminded on elapsed time rather than poll
# count. A dropped remediation turn can therefore delay, but not permanently
# consume, the signal without making normal 15-minute polls noisy.
CHANGES_REQUESTED_REMINDER_INTERVAL = timedelta(minutes=60)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cursor(cursor: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(
        json.dumps(cursor, indent=2), encoding="utf-8",
    )


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


def _gh_api(endpoint: str, token: str) -> list | dict | None:
    """Call ``gh api <endpoint> --paginate`` and return parsed JSON.
    Returns None on error so callers can skip silently."""
    try:
        env = {**os.environ, "GH_TOKEN": token} if token else None
        result = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True, text=True, timeout=30, env=env,
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
    if isinstance(event_type, str) and event_type in REVIEW_NEEDED_EVENT_TYPES:
        prompt = prompt + _REVIEW_SUBMISSION_RULE
        preload = os.environ.get("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "").strip().lower()
        if preload in ("1", "true", "yes"):
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


# ─── per-resource checks ──────────────────────────────────────────────


def _check_issues(repo: str, since: str, token: str, me: str) -> int:
    """New issues (NOT PRs — GitHub's /issues endpoint returns both;
    we filter PRs out via the ``pull_request`` field)."""
    data = _gh_api(
        f"repos/{repo}/issues?state=open&since={since}"
        f"&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
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


def _check_prs(repo: str, since: str, token: str, me: str) -> int:
    """New pull requests."""
    data = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
        return 0
    count = 0
    for pr in data:
        if me and pr.get("user", {}).get("login") == me:
            continue
        if (pr.get("created_at", "") or "") <= since:
            continue
        author = pr.get("user", {}).get("login", "unknown")
        number = pr.get("number")
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        body = _truncate(pr.get("body") or "")
        prompt_parts = [f"New PR on {repo}: #{number} {title} (by @{author})"]
        if body:
            prompt_parts.append(body)
        prompt_parts.append(url)
        _emit("\n".join(prompt_parts), event_type="pr_opened",
              repo=repo, number=number, url=url, author=author,
              head_sha=(pr.get("head") or {}).get("sha"))
        count += 1
    return count


def _check_issue_comments(repo: str, since: str, token: str, me: str) -> int:
    """New issue + PR conversation comments (the
    /repos/{repo}/issues/comments endpoint covers both)."""
    data = _gh_api(
        f"repos/{repo}/issues/comments?since={since}"
        f"&sort=created&direction=desc",
        token,
    )
    if not isinstance(data, list):
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
        issue_url = comment.get("issue_url", "")
        issue_num = (
            issue_url.rstrip("/").split("/")[-1] if issue_url else "?"
        )
        prompt = (
            f"New comment on {repo} #{issue_num} by @{author}: {body}\n{url}"
        )
        _emit(prompt, event_type="issue_comment",
              repo=repo, number=issue_num, url=url, author=author)
        count += 1
    return count


def _check_pr_review_comments(repo: str, since: str, token: str, me: str) -> int:
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
        _emit(prompt, event_type="pr_review_comment",
              repo=repo, number=pr_num, url=url, path=path, author=author)
        count += 1
    return count


def _check_pr_pushes(
    repo: str,
    token: str,
    me: str,
    pr_heads: dict[str, str],
    pr_review_requests: dict[str, int] | None = None,
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
    where ``attempt_count`` is how many ``pr_review_requested`` events
    we've emitted for this PR while ``me`` stayed requested.

    While ``me`` is a requested reviewer the poller RE-EMITS
    ``pr_review_requested`` once per poll (incrementing the count), up to
    ``REVIEW_REQUEST_MAX_ATTEMPTS``. Rationale: a submitted review removes
    ``me`` from ``requested_reviewers`` (GitHub clears it), so "still
    requested on the next poll" means "no review landed" — the prior
    attempt's turn failed, is still running, or never ran. Re-emitting
    recovers a review dropped by a transient turn failure (the bug: the
    old emit-once-on-transition model recorded the request as seen, so a
    dead turn vanished — PR #511).

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

        # ─── pr_synchronize (push detection) ───
        current_sha = (pr.get("head") or {}).get("sha")
        if current_sha and (not me or pr_author != me):
            prev_sha = pr_heads.get(key)
            if prev_sha is None:
                # First sighting — record, do not emit.
                new_heads[key] = current_sha
            elif prev_sha != current_sha:
                title = pr.get("title", "")
                url = pr.get("html_url", "")
                # Fetch commit list between previous and new head so the
                # agent sees individual commit subjects, not just a sha
                # delta.  GitHub caps the ``commits`` array at 250 items;
                # ``ahead_by`` is the canonical count when the list is
                # truncated.  On API failure we fall back to sha-only.
                compare = _gh_api(
                    f"repos/{repo}/compare/{prev_sha}...{current_sha}",
                    token,
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
                        (c.get("commit") or {}).get("message", "")
                        .split("\n")[0][:72]
                        for c in commits[:3]
                    ]
                    bullets = "\n".join(
                        f"  • {s}" for s in subjects if s
                    )
                    shown = sum(1 for s in subjects if s)
                    remaining = total_commits - shown
                    commit_block = f"{total_commits} commit(s):\n{bullets}"
                    if remaining > 0:
                        commit_block += f"\n  • … ({remaining} more)"
                else:
                    commit_block = "(commit details unavailable)"
                prompt = (
                    f"PR #{number} updated on {repo}: {title} "
                    f"(by @{push_author or 'unknown'})\n"
                    f"{commit_block}\n"
                    f"Previous head: {prev_sha[:8]}, new head: "
                    f"{current_sha[:8]}\n{url}"
                )
                _emit(
                    prompt,
                    event_type="pr_synchronize",
                    repo=repo,
                    number=number,
                    url=url,
                    previous_head=prev_sha,
                    new_head=current_sha,
                    head_sha=current_sha,
                    author=push_author,
                )
                count += 1
                new_heads[key] = current_sha
            else:
                new_heads[key] = current_sha

        # ─── pr_review_requested (reviewer added) ───
        # Skip if no agent login configured — nothing to match against.
        if me:
            requested = pr.get("requested_reviewers") or []
            currently_requested = any(
                isinstance(r, dict) and r.get("login") == me
                for r in requested
            )
            if currently_requested:
                # State reconciliation (chainlink #299): ``me`` being a
                # requested reviewer is usually the authoritative "review
                # still pending" signal. Exception (chainlink #669): an
                # operator can re-request the reviewer after a completed
                # review at the current head; in that case the request is
                # already satisfied, so drop it from the retry cursor.
                if current_sha and _has_current_head_review(
                    repo, number, current_sha, me, token
                ):
                    continue

                # Otherwise, a PR STILL requested on this poll means the
                # prior attempt never landed (transient turn failure / still
                # running / never ran). Re-emit up to the cap; on exhaustion
                # emit a one-shot give-up signal and go dormant.
                prior_attempts = prior_review_requests.get(key, 0)
                title = pr.get("title", "")
                url = pr.get("html_url", "")
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
                    prompt = (
                        f"Review requested on {repo} PR #{number}: "
                        f"{title} (by @{pr_author or 'unknown'})\n"
                        f"{status_line}\n"
                        f"{url}"
                    )
                    _emit(
                        prompt,
                        event_type="pr_review_requested",
                        repo=repo,
                        number=number,
                        url=url,
                        requested_reviewer=me,
                        author=pr_author,
                        attempt=attempt,
                        max_attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
                        head_sha=current_sha,
                    )
                    count += 1
                    new_review_requests[key] = attempt
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
                        attempts=REVIEW_REQUEST_MAX_ATTEMPTS,
                    )
                    count += 1
                    new_review_requests[key] = prior_attempts + 1
                else:
                    # Already gave up (sentinel > cap) and ``me`` is still
                    # requested. Stay dormant — carry the sentinel so we
                    # neither retry nor re-emit the give-up. Resets when
                    # ``me`` is removed (key drops from the rebuilt dict).
                    new_review_requests[key] = prior_attempts
    return count, new_heads, new_review_requests


def _has_current_head_review(
    repo: str,
    number: int,
    head_sha: str,
    reviewer: str,
    token: str,
) -> bool:
    """Return whether ``reviewer`` has submitted a review at ``head_sha``.

    GitHub normally clears a reviewer from ``requested_reviewers`` when they
    submit a review, but an operator can re-request the same reviewer after a
    completed current-head review. Treat APPROVED, CHANGES_REQUESTED, and
    COMMENTED as substantive submitted reviews so the review-request retry
    loop does not page on an already-completed review. API failures return
    False so the existing retry path still recovers genuinely missed reviews.
    """
    if not head_sha or not reviewer:
        return False
    data = _gh_api(f"repos/{repo}/pulls/{number}/reviews", token)
    if not isinstance(data, list):
        return False
    substantive = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    for review in data:
        if not isinstance(review, dict):
            continue
        login = (review.get("user") or {}).get("login")
        state = str(review.get("state") or "").upper()
        commit_id = review.get("commit_id")
        if login == reviewer and commit_id == head_sha and state in substantive:
            return True
    return False

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

    Reminder contract: ``prior`` maps each PR key to ``head_sha`` plus
    ``last_reminded_at``. An unresolved stale state re-emits only after
    ``reminder_interval`` has elapsed. The head is observational rather
    than the resolution key: a content-free rebase carries the existing
    reminder timestamp forward, while the reviewed-diff check below drops
    a substantive fix. Legacy bare-sha entries migrate quietly with
    ``last_reminded_at=now`` so deployment does not produce a reminder
    storm. Cleanup remains rebuild-on-every-poll: closed, merged, fixed,
    and no-longer-blocked PRs are not copied into the returned dict.

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
    for pr in data:
        if (pr.get("user") or {}).get("login") != me:
            continue
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha") or ""
        base_sha = (pr.get("base") or {}).get("sha") or ""
        if not number or not head_sha:
            continue
        key = str(number)
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
            # Pre-cadence cursor. Treat its prior one-shot reminder as if it
            # happened now; only a full interval after migration may re-fire.
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": observed_at_iso,
            }
            continue

        last_reminded_at = ""
        last_reminded: datetime | None = None
        if isinstance(prior_entry, dict):
            value = prior_entry.get("last_reminded_at")
            if isinstance(value, str):
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

        if last_reminded is not None and observed_at - last_reminded < reminder_interval:
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": last_reminded_at,
            }
            continue
        if key in prior and last_reminded is None:
            # Quietly repair malformed structured state just like a legacy
            # entry rather than turning cursor corruption into an emit storm.
            new[key] = {
                "head_sha": head_sha,
                "last_reminded_at": observed_at_iso,
            }
            continue

        title = pr.get("title", "")
        url = pr.get("html_url", "")
        reviewers = ", ".join(f"@{login}" for login in sorted(blocking))
        prompt = (
            f"Your PR #{number} on {repo} is stuck at CHANGES_REQUESTED: "
            f"{title}\n"
            f"{reviewers} requested changes and no commits have landed "
            f"since (head {head_sha[:8]}). Address the review feedback, "
            f"push the fixes, and re-request review.\n{url}"
        )
        _emit(
            prompt,
            event_type="pr_changes_requested_stale",
            repo=repo,
            number=number,
            url=url,
            head=head_sha,
            reviewers=sorted(blocking),
            author=(pr.get("user") or {}).get("login"),
        )
        count += 1
        new[key] = {
            "head_sha": head_sha,
            "last_reminded_at": observed_at_iso,
        }
    return count, new


def _check_pr_reviews(repo: str, since: str, token: str, me: str) -> int:
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
        return 0
    count = 0
    for pr in prs:
        pr_number = pr.get("number")
        if not pr_number:
            continue
        reviews = _gh_api(f"repos/{repo}/pulls/{pr_number}/reviews", token)
        if not isinstance(reviews, list):
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
            author = review.get("user", {}).get("login", "unknown")
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
                f"@{author} {state_label} PR #{pr_number} "
                f"({pr_title}) on {repo}"
            )
            if body:
                prompt += f"\n{body}"
            prompt += f"\n{url}"
            _emit(prompt, event_type="pr_review",
                  repo=repo, number=pr_number, url=url, state=state,
                  author=author)
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
    # ``{repo: {pr_key: {head_sha, last_reminded_at}}}`` — unresolved
    # own-PR states, rate-limited by elapsed time between reminders.
    cr_all: dict = cursor.get("pr_changes_requested", {}) or {}
    new_cr_all: dict[str, dict[str, object]] = {}

    total = 0
    for repo in repos:
        print(f"Checking {repo} since {since}...", file=sys.stderr)
        total += _check_issues(repo, since, token, me)
        total += _check_prs(repo, since, token, me)
        total += _check_issue_comments(repo, since, token, me)
        total += _check_pr_review_comments(repo, since, token, me)
        total += _check_pr_reviews(repo, since, token, me)
        repo_heads = pr_heads_all.get(repo, {}) or {}
        repo_rr = _coerce_review_requests(rr_all.get(repo))
        push_count, new_repo_heads, new_repo_rr = _check_pr_pushes(
            repo, token, me, repo_heads, pr_review_requests=repo_rr,
        )
        total += push_count
        new_pr_heads_all[repo] = new_repo_heads
        new_rr_all[repo] = new_repo_rr
        repo_cr = cr_all.get(repo, {}) or {}
        cr_count, new_repo_cr = _check_own_changes_requested(
            repo, token, me, repo_cr,
        )
        total += cr_count
        new_cr_all[repo] = new_repo_cr

    cursor["last_checked"] = new_cursor_ts
    cursor["pr_heads"] = new_pr_heads_all
    cursor["pr_review_requests"] = new_rr_all
    cursor["pr_changes_requested"] = new_cr_all
    _save_cursor(cursor)
    print(
        f"Emitted {total} event(s) across {len(repos)} repo(s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
