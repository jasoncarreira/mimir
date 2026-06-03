#!/usr/bin/env python3
"""GitHub Actions CI watcher — pollers.json contract.

Watches the main branch of each GITHUB_REPOS entry for new workflow
run failures. Emits one JSONL event per newly-failed run; stays silent
when all runs pass.

The "silence as filter" principle: this poller only speaks when CI
breaks. Green builds produce zero output.

Environment variables:
    STATE_DIR     - Persistent state dir (set by framework)
    GITHUB_REPOS  - Comma-separated owner/repo list (REQUIRED)
    GITHUB_TOKEN  - Optional; falls back to ``gh auth token``

Output contract:
    stdout: JSONL — {"poller": str, "prompt": str, ...} per event
    stderr: diagnostic logging
    exit 0: success (zero events fine — silence = CI is green)
    non-zero: error (framework drops emitted events for the run)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
SEEN_FILE = STATE_DIR / "seen_run_ids.json"
POLLER_NAME = "github-ci-watch"

# How many recent runs to inspect per repo per branch.
RUNS_TO_CHECK = 10
BRANCH = "main"

# Conclusions that indicate a broken build.
FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _load_seen() -> set[int]:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            return set(data.get("ids", []))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _save_seen(ids: set[int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep only the last 200 seen IDs to prevent unbounded growth.
    trimmed = sorted(ids)[-200:]
    SEEN_FILE.write_text(
        json.dumps({"ids": trimmed}), encoding="utf-8"
    )


def _gh(*args: str) -> dict | list | None:
    """Run gh CLI and return parsed JSON, or None on error."""
    token = os.environ.get("GITHUB_TOKEN", "")
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            _log(f"gh error: {result.stderr.strip()}")
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        _log(f"gh exception: {e}")
        return None


def _check_repo(repo: str, seen: set[int]) -> list[int]:
    """Check repo for new CI failures on main. Returns list of newly seen IDs."""
    runs = _gh(
        "run", "list",
        "--repo", repo,
        "--branch", BRANCH,
        "--limit", str(RUNS_TO_CHECK),
        "--json", "databaseId,status,conclusion,name,workflowName,createdAt,url",
    )
    if runs is None:
        return []

    newly_seen: list[int] = []
    for run in runs:
        run_id = run.get("databaseId")
        if run_id is None:
            continue

        status = run.get("status", "")
        conclusion = run.get("conclusion", "")

        # Only mark a run "seen" once it has COMPLETED (chainlink #307). An
        # in-progress / queued run observed now concludes later — recording
        # it as seen HERE meant its eventual failure was silently skipped on
        # the next poll (it was already in ``seen``). A non-terminal run is
        # left UNSEEN so it's re-checked each poll until it concludes, at
        # which point a failure still emits.
        if status != "completed":
            continue
        # Completed → record for the seen-set (whether new or already-seen,
        # so the caller's union + cap keeps it).
        newly_seen.append(run_id)

        if run_id in seen:
            continue  # already reported

        # A successful / skipped / cancelled run is recorded as seen above
        # (so we don't re-check it) but isn't alerted — only failing ones.
        if conclusion in FAILURE_CONCLUSIONS:
            workflow = run.get("workflowName") or run.get("name") or "unknown"
            created = run.get("createdAt", "")
            url = run.get("url", "")
            _emit({
                "poller": POLLER_NAME,
                "event_type": "ci_failure",
                "repo": repo,
                "branch": BRANCH,
                "workflow": workflow,
                "conclusion": conclusion,
                "run_id": run_id,
                "created_at": created,
                "url": url,
                "prompt": (
                    f"CI failure on {repo} main branch: "
                    f"workflow '{workflow}' {conclusion} "
                    f"(run {run_id}, {created}). "
                    f"URL: {url}"
                ),
            })
            _log(f"Emitted failure: {repo} {workflow} run {run_id}")

    return newly_seen


_STATE_GITIGNORE = """\
# Transient github-ci-watch state — seeded by the github-ci-watch skill
# (write-if-missing; edit freely). The seen-run-ids dedup set churns every
# poll and has no audit value; per-directory .gitignore keeps it out of the
# home's tracked git history.
seen_run_ids.json
*.tmp
"""


def _seed_state_gitignore() -> None:
    """Seed STATE_DIR/.gitignore (only if absent) so the poller's transient
    seen-ids set isn't committed to the home repo. Best-effort; never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gi = STATE_DIR / ".gitignore"
        if not gi.exists():
            gi.write_text(_STATE_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    _seed_state_gitignore()
    repos_raw = os.environ.get("GITHUB_REPOS", "").strip()
    if not repos_raw:
        _log("GITHUB_REPOS not set — nothing to watch")
        return 1

    repos = [r.strip() for r in repos_raw.split(",") if r.strip()]
    seen = _load_seen()
    all_seen_this_run: list[int] = []

    for repo in repos:
        _log(f"Checking {repo} {BRANCH} CI...")
        new_ids = _check_repo(repo, seen)
        all_seen_this_run.extend(new_ids)

    # Update seen set: union of prior + all IDs observed this run.
    _save_seen(seen | set(all_seen_this_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
