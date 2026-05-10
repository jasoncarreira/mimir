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


def _emit(prompt: str, **extras: object) -> None:
    """One JSONL event line — framework parses + delivers as
    AgentEvent. ``source_platform`` flows through for prompt
    rendering."""
    event = {
        "poller": POLLER_NAME,
        "source_platform": "github",
        "prompt": prompt,
        **extras,
    }
    print(json.dumps(event), flush=True)


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
              repo=repo, number=number, url=url)
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
              repo=repo, number=number, url=url)
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
              repo=repo, number=issue_num, url=url)
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
              repo=repo, number=pr_num, url=url, path=path)
        count += 1
    return count


def _check_pr_pushes(
    repo: str,
    token: str,
    me: str,
    pr_heads: dict[str, str],
) -> tuple[int, dict[str, str]]:
    """Detect new commits pushed to existing open PRs.

    Different signature from the sibling checks: takes ``pr_heads``
    (the per-repo ``{number_str: sha}`` snapshot from the previous
    poll, possibly empty) instead of a ``since`` timestamp, and
    returns ``(emit_count, new_pr_heads)``. The cleanup model is
    "rebuild ``pr_heads`` from the current ``state=open`` set on
    every poll" — closed/merged PRs and PRs in repos no longer in
    the watch list naturally drop out because they're never copied
    into the new dict the caller saves.

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
    """
    data = _gh_api(
        f"repos/{repo}/pulls?state=open&sort=created&direction=desc",
        token,
    )
    new_heads: dict[str, str] = {}
    if not isinstance(data, list):
        # On API failure, preserve prior heads so we don't false-fire
        # on the next successful poll. (If the poll truly missed a
        # push, we'll catch it next time.)
        return 0, dict(pr_heads)
    count = 0
    for pr in data:
        if me and pr.get("user", {}).get("login") == me:
            continue
        number = pr.get("number")
        if not number:
            continue
        current_sha = (pr.get("head") or {}).get("sha")
        if not current_sha:
            continue
        key = str(number)
        prev_sha = pr_heads.get(key)
        if prev_sha is None:
            # First sighting — record, do not emit.
            new_heads[key] = current_sha
            continue
        if prev_sha != current_sha:
            author = pr.get("user", {}).get("login", "unknown")
            title = pr.get("title", "")
            url = pr.get("html_url", "")
            prompt = (
                f"PR #{number} updated on {repo}: {title} (by @{author})\n"
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
            )
            count += 1
        new_heads[key] = current_sha
    return count, new_heads


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
                  repo=repo, number=pr_number, url=url, state=state)
            count += 1
    return count


# ─── main ─────────────────────────────────────────────────────────────


def main() -> None:
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

    total = 0
    for repo in repos:
        print(f"Checking {repo} since {since}...", file=sys.stderr)
        total += _check_issues(repo, since, token, me)
        total += _check_prs(repo, since, token, me)
        total += _check_issue_comments(repo, since, token, me)
        total += _check_pr_review_comments(repo, since, token, me)
        total += _check_pr_reviews(repo, since, token, me)
        repo_heads = pr_heads_all.get(repo, {}) or {}
        push_count, new_repo_heads = _check_pr_pushes(
            repo, token, me, repo_heads,
        )
        total += push_count
        new_pr_heads_all[repo] = new_repo_heads

    cursor["last_checked"] = new_cursor_ts
    cursor["pr_heads"] = new_pr_heads_all
    _save_cursor(cursor)
    print(
        f"Emitted {total} event(s) across {len(repos)} repo(s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
