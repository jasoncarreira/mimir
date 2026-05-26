---
name: github-poller
description: Watch one or more GitHub repositories for new issues, pull requests, comments, PR reviews, and inline diff comments — emits a fresh turn each time something actionable happens. Use when you've opened PRs / issues mimir should know about review activity for, when you want operator-actionable signals for repos mimir maintains, or when you need to track upstream activity on a watched repo. Filters out events authored by mimir's own GitHub identity (set ``MIMIR_GITHUB_SELF_LOGIN``) so the bot doesn't wake itself with its own comments. Companion to the ``pollers`` framework skill (mechanics) and the ``world-scanning`` skill (catalog of *what's worth polling*). Opt-in: copy this directory into ``<home>/.claude/skills/github-poller/`` then set the env vars below.
---

# github-poller — watch GitHub repos for activity

This is an **opt-in poller skill** that ships with mimir under
``mimir/optional-skills/`` but is NOT auto-installed. Most installs
won't watch a GitHub repo, so the framework doesn't seed it by default.

## Installation

1. Copy the directory into your agent home:
   ```
   cp -r mimir/optional-skills/github-poller <home>/.claude/skills/
   ```
   (Or run from inside the container against ``/workspace/mimir``.)

2. Configure env vars:

   | Variable | Required | Description |
   |---|---|---|
   | `GITHUB_REPOS` | yes | Comma-separated `owner/repo` list (e.g. `jasoncarreira/mimir,jasoncarreira/mimirbot`) |
   | `GITHUB_TOKEN` | recommended | GitHub PAT. Falls back to `gh auth token` when unset. |
   | `MIMIR_GITHUB_SELF_LOGIN` | optional | GitHub login mimir authors as. Events from this login get filtered out (mimir doesn't wake itself). Leave empty if mimir uses the operator's PAT — the operator is the signal you want, not the noise. |

   These env vars must be exported in mimir's process environment
   (e.g. via the container's env file). The framework gates subprocess
   env keys two ways: a built-in allowlist (``PATH``, ``HOME``,
   locale, XDG, CA bundles — everything else is stripped) plus a deny
   filter on top (``*_TOKEN``, ``*_SECRET``, ``*_API_KEY``,
   ``*_PASSWORD``, ``MIMIR_*`` are stripped even if allowlisted).
   The ``pass_env`` field in this skill's ``pollers.json`` declares
   all three keys above as explicit pass-throughs, bypassing both
   gates — different mechanism per key:
   - ``GITHUB_TOKEN`` — matches ``*_TOKEN`` deny pattern → `pass_env`
     bypasses the deny filter.
   - ``MIMIR_GITHUB_SELF_LOGIN`` — matches ``MIMIR_*`` deny pattern →
     `pass_env` bypasses the deny filter.
   - ``GITHUB_REPOS`` — not a secret, but not in the built-in
     allowlist either → `pass_env` bypasses the allowlist gate. Same
     end result (key reaches the subprocess), just gated differently.
   If you rename or relocate a var, update ``pollers.json``
   accordingly. ``GITHUB_TOKEN`` set but not declared in ``pass_env``
   → silently absent in the subprocess → poller falls through to
   ``gh auth token`` → zero events forever if ``gh`` isn't authed.

3. Bring it live:
   ```
   reload_pollers
   # → "reload_pollers ok: N poller(s) registered — github-activity, ..."
   ```
   (Or restart the container; pollers are auto-loaded at startup.)

## What it watches

For each repo in `GITHUB_REPOS`:

- **New issues** opened (since last poll)
- **New pull requests** opened
- **New issue comments** (covers both issue + PR conversation comments)
- **New PR review comments** (inline diff comments — distinct from conversation comments)
- **New PR reviews** (approve / changes-requested / commented with body)
- **PR pushes/updates** (`pr_synchronize`) — new commits pushed to an existing open PR.
  The poller calls `GET /repos/{repo}/compare/{prev}...{new}` to fetch up to 3
  commit subjects inline. If multiple commits land between polls, all are
  surfaced (total count + first 3 subjects + "… N more" when truncated).
  Force-pushes that change the SHA but not the diff also fire — this is a
  known false-positive; compare-diff diffing on every poll is too expensive.

All filtered by `created_at > cursor` and (when set) `user.login != MIMIR_GITHUB_SELF_LOGIN`.

## What it doesn't watch (deliberate)

- **Commits** — already handled by `git pull`. No GitHub-API path for "new commit" wakes today.
- **Issue/PR state changes** (close, reopen, merge, label) — adds noise; revisit if it becomes useful.
- **Workflow runs / check failures** — separate concern; would warrant its own poller.
- **Notifications API** (`/notifications`) — an alternative path that's higher-noise; this poller takes the targeted-endpoint approach instead.

## Why login-based, not email-based

GitHub's API events expose `user.login` for issues, PRs, comments, and reviews — never the author's commit email. Email-based filtering (e.g. `noreply@mimir-agent.local`) is the right key for **commits** but not for the API surface.

## Cost

Polling 1 repo every 15 min @ 4 endpoints = ~16 calls/hr/repo. For a 5-repo watch that's ~80/hr — well under GitHub's 5000/hr per-PAT rate limit. The `since=` query param keeps each call's payload small.

## Batching

`pollers.json` sets `batch_size=5`: GitHub items emitted in a single cron tick get coalesced into AgentEvents of up to 5 items each. A typical 1-3-event tick collapses to 1 turn; a 12-event backfill (e.g. after a cursor sync past a busy PR-review hour) splits into 3 turns (5 + 5 + 2) instead of firing 12 separate turns. Each batched turn renders as a numbered list with a `<poller> reported N items (batch X of Y)` header so the agent can scan the batch + react per-item, and `extra.items[]` carries per-item metadata (URLs, refs) for programmatic access. Tune via `batch_size` in `pollers.json` if your repos generate different event volumes.

## Cursor file

Persists at `<home>/state/pollers/github-activity/cursor.json` (the framework-injected `STATE_DIR`). Survives container rebuilds. First run looks back 1 hour to bound the backfill burst.

## Disabling temporarily

- Unset `GITHUB_REPOS` — the poller exits silently.
- Or set the cron in `pollers.json` to a far-future expression.
- Or remove the skill directory: `rm -rf <home>/.claude/skills/github-poller/` + `reload_pollers`.

## Debugging

| Symptom | Check |
|---|---|
| Poller not firing | `events.jsonl` for `poller_complete{poller=github-activity}` — is it appearing on schedule? |
| No events from a repo where you expected one | `events.jsonl` for `poller_stderr` from this poller — auth or rate-limit issues land here |
| Too many wake-ups | Set `MIMIR_GITHUB_SELF_LOGIN` to mimir's GitHub login (when mimir gets its own bot account) so its own comments stop firing the poller |
| Cursor stuck | `cat <home>/state/pollers/github-activity/cursor.json` — should be a recent ISO timestamp |
