---
name: github-ci-watch
description: Watch the main branch of one or more GitHub repositories for new CI (GitHub Actions) workflow-run failures — emits a fresh turn the moment a build breaks and stays silent while CI is green. Use when mimir maintains repos whose CI it should react to (e.g. a failing build on main after a merge it landed). Seen-set deduped so each failed run is reported exactly once. Companion to the ``pollers`` framework skill (mechanics) and the ``world-scanning`` skill (catalog of what's worth polling). Opt-in: copy this directory into ``<home>/.claude/skills/github-ci-watch/`` then set the env vars below.
env:
  required:
    - name: GITHUB_REPOS
      description: "Comma-separated owner/repo list to watch CI on (e.g. jasoncarreira/mimir,jasoncarreira/mimirbot)."
      example: "jasoncarreira/mimir"
  optional:
    - name: GITHUB_TOKEN
      description: "GitHub PAT with repo scope. Falls back to ``gh auth token`` when unset."
      example: "ghp_..."
---

# github-ci-watch — react when CI breaks

An **opt-in poller skill** that ships with mimir under
``mimir/optional-skills/`` but is NOT auto-installed — most installs
don't watch a repo's CI, so the framework doesn't seed it by default.

It watches the ``main`` branch of each ``GITHUB_REPOS`` entry, inspects
the last few GitHub Actions workflow runs, and emits one ``ci_failure``
event per newly-failed run (conclusion ``failure`` / ``timed_out`` /
``startup_failure``). Green builds produce **zero output** — the
"silence as filter" principle: this poller only speaks when CI breaks.

## Installation

1. Copy the directory into your agent home:
   ```
   cp -r mimir/optional-skills/github-ci-watch <home>/.claude/skills/
   ```
   (Or run from inside the container against ``/workspace/mimir``.)

2. Set the env vars — at minimum ``GITHUB_REPOS`` (plus ``GITHUB_TOKEN``
   if ``gh auth`` isn't already configured in the environment).

3. The ``pollers`` framework discovers ``pollers.json`` and runs
   ``poller.py`` on its cron (default ``*/15 * * * *``).

## State

Reported run IDs are tracked in ``seen_run_ids.json`` (trimmed to the
last 200) so a given failure is reported exactly once. State lives in
``$STATE_DIR`` (resolved by the framework to a persistent per-poller
location that survives container rebuilds).

## Output

One JSONL event per new failure:
``{event_type: "ci_failure", repo, branch, workflow, conclusion,
run_id, created_at, url, prompt}`` — the ``prompt`` is the turn the
agent wakes on.
