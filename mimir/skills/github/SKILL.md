---
name: github
description: Interact with GitHub via the `gh` CLI. Use for issues (read, comment, file), pull requests (view, comment, check CI status), workflow runs, and `gh api` for queries the subcommands don't cover. Authentication is via the bot's GITHUB_TOKEN (already wired through `gh auth setup-git` at container start).
success_criteria:
  # The skill exists to wrap gh CLI invocations. Loading the SKILL.md
  # and not running any gh command means we read the policy but
  # didn't actually act on it.
  any_of:
    - tool_call:
        name: Bash
        args:
          command_glob: "gh *"
    - tool_call:
        name: Bash
        args:
          command_glob: "* gh *"
---

<!-- desc: Interact with GitHub via the gh CLI — issues, pull requests, CI status, workflow runs, and gh api for uncovered queries. -->

# GitHub

The `gh` CLI is installed in the container and authenticated via `GITHUB_TOKEN`
from the env. Commits the agent makes through `git push` go through the same
credential helper, so `gh pr create` and `git push` both work.

## Contract

**Trigger**: Any interaction with GitHub that goes beyond local git — reading issues
or PRs, filing comments, checking CI, querying the API. If the task requires GitHub
state (not just local repo state), invoke this skill.

**Requires**: `GITHUB_TOKEN` in environment (verified by `gh auth status`). The PAT's
scope determines which repos are reachable — operations on out-of-scope repos return
403; check what the operator configured if you hit one.

**Guarantees**:
- GitHub interactions happen via `gh` CLI (authenticated, auditable), not via
  WebFetch on raw GitHub URLs.
- PAT scope is checked mentally before any cross-repo operation — a 403 from an
  out-of-scope repo is expected behavior, not a bug.

**Does not**: Push code (that's `git push`, separate from `gh`); manage GitHub Actions
billing; access repos outside the PAT scope; file issues on external repos without
operator approval.

When `cwd` is inside a git checkout of the target repo, you don't need `--repo`
— gh resolves the upstream remote from the local `origin`. When working from
elsewhere or operating on a different repo, pass `--repo owner/repo` (or use a
URL).

## Pre-merge CHANGES_REQUESTED gate (chainlink #214, rule via #217)

**Before invoking `gh pr merge` on any PR, ALWAYS check the current review
state.** A reviewer can flip to CHANGES_REQUESTED after the first approval —
calling `gh pr merge` without a fresh check can merge a PR that has outstanding
blocking feedback.

```bash
# Returns the login list of blocking reviewers (empty array [] if none)
BLOCKING=$(gh pr view <PR> --json reviews --jq '
  [.reviews | group_by(.author.login)[] | sort_by(.submittedAt)[-1]
   | select(.state == "CHANGES_REQUESTED") | .author.login]')

# Refuse if non-empty
if [ "$BLOCKING" != "[]" ]; then
    # blocked — do not merge; see steps below
    echo "Merge blocked by CHANGES_REQUESTED from: $BLOCKING"
fi
```

If `$BLOCKING` is non-empty (one or more reviewers in CHANGES_REQUESTED state):

1. **Refuse the merge** — do not call `gh pr merge`.
2. Post a `gh pr comment` explaining why the merge was blocked and listing the
   blocking reviewers from `$BLOCKING` who need to re-approve.

This is a non-negotiable hard gate — not a heuristic. "Operator approved once"
is not sufficient if a subsequent CHANGES_REQUESTED arrived, even seconds later.
(Structured event emission for blocked merges is tracked in chainlink #218.)

## Pre-push staleness gate (chainlink #219)

**Before pushing a PR branch for re-review, ALWAYS check that the branch is
current against `origin/main`.** Between the last rebase and the next push,
other PRs may have merged to `main`; a stale base makes the PR diff include
complete reverts of unrelated landed work. A squash-merge would silently drop
those commits from `main`.

```bash
# Step 1: fetch and check staleness
git fetch origin main
MERGE_BASE=$(git merge-base HEAD origin/main)
MAIN_TIP=$(git rev-parse origin/main)

if [ "$MERGE_BASE" != "$MAIN_TIP" ]; then
    echo "Branch is stale — rebasing onto current origin/main"
    git rebase origin/main
fi

# Step 2: inspect paths in the diff after rebase
git diff origin/main..HEAD --name-only
```

After fetching (and rebasing if stale), inspect the path list:

- If every path is **within the PR's declared scope** (the chainlink ID, PR
  title, or known feature area), the push is safe — proceed.
- If any path is **outside scope** (a revert of unrelated work, or an
  accidentally-staged file), stop — do not push. Investigate whether a further
  rebase is needed or whether a stale commit slipped onto the branch.

**Non-negotiable rule**: do not push to a PR branch for re-review without this
check. The cost of missing a stale base is silently reverting landed work when
the PR is squash-merged. Pairs with the pre-merge gate above — both gates exist
because the window between "last rebase" and "reviewer sees diff" is enough for
main to advance.

Four concrete instances from 2026-05-27 that this gate would have caught:
#393 (would have wiped #391/#388/#389/#392), #394 round 1 (stacked on stale
#393), #396 (would have reverted #395 ~520 lines), #394 round 2 (stale base
after #395 merged).

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
