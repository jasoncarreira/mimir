---
name: github
description: Interact with GitHub via the `gh` CLI. Use for issues (read, comment, file), pull requests (view, comment, check CI status), workflow runs, and `gh api` for queries the subcommands don't cover. Authentication is via the bot's GITHUB_TOKEN (already wired through `gh auth setup-git` at container start).
allowed-tools:
  - Bash
---

# GitHub

The `gh` CLI is installed in the container and authenticated via `GITHUB_TOKEN`
from the env. Commits the agent makes through `git push` go through the same
credential helper, so `gh pr create` and `git push` both work.

When inside `/workspace/mimir/` (the cloned repo) you don't need `--repo` —
gh resolves the upstream remote. When working from elsewhere or operating on
a different repo, pass `--repo owner/repo` (or use a URL).

## Pull requests

```bash
gh pr list --state open                       # open PRs on the current repo
gh pr view 42                                  # view a PR
gh pr view 42 --json title,state,reviews       # structured fields
gh pr checks 42                                # CI status
gh pr create --title "..." --body "..."        # open a PR (uses HEREDOC for body)
gh pr comment 42 --body "looking now"
gh pr diff 42                                  # the PR's diff
```

For PRs the bot opens, the convention (see mimirbot/README.md) is feature
branch + PR; do not push to `main` directly.

## Issues

```bash
gh issue list --state open
gh issue view 7
gh issue comment 7 --body "ack — picking this up"
gh issue create --title "..." --body "..."     # only when explicitly asked
```

## Workflow runs / CI

```bash
gh run list --limit 10
gh run view <run-id>                           # which steps passed/failed
gh run view <run-id> --log-failed              # logs for failed steps only
gh run watch <run-id>                          # block until done
```

## `gh api` for advanced queries

When the subcommands don't cover what you need, drop to the API:

```bash
gh api repos/jasoncarreira/mimir/pulls/42 --jq '.title, .state, .user.login'
gh api repos/jasoncarreira/mimir/pulls/42/comments
gh api 'repos/jasoncarreira/mimir/issues?state=open&labels=bug' --jq '.[] | "\(.number): \(.title)"'
```

`--jq` filters work like inline jq queries; chain with shell tools when needed.

## When NOT to use this

- Don't push to `main` even when you can — feature branch + PR is the
  convention. Operator merges.
- Don't open issues without being asked; reading and commenting is the
  default mode.
- Don't use `gh repo delete` or `gh release delete` ever (no destructive ops).
