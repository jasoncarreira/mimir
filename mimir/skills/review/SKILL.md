---
name: review
description: Review a pull request and POST the review to GitHub via `gh pr review`. Use when asked to review a PR, or when the github-activity poller fires a PR-opened / PR-review-requested event. Always submits after drafting — a review that stays in turn output and never reaches GitHub is a non-review.
---

<!-- desc: Review a pull request and POST the review to GitHub via gh pr review — always submits after drafting. -->

# PR Review

Read the diff, draft a verdict, and **submit it via `gh pr review`**. The
submission step is not optional — it is the definition of "done" for this
skill.

## Contract

**Trigger**: Operator asks to review a PR; or the github-activity poller fires a
`pr_opened` / `review_requested` event for a PR authored by someone other than
the agent itself (`$MIMIR_GITHUB_SELF_LOGIN`) — the agent doesn't self-review.

**Requires**: PR number; `gh` CLI authenticated (verified by `gh auth status`);
diff accessible — either under 20k lines via `gh pr diff`, or file list via
`gh pr view <n> --repo <owner>/<repo> --json files` for large PRs (`gh api` is
not admitted on a poller turn).

**Guarantees**:
- A review is posted to GitHub (not just output to turn text) via `gh pr review`.
- Verdict is one of APPROVE / REQUEST_CHANGES / COMMENT — never left unsubmitted.
- Every blocking finding is anchored to a specific file + line in the diff.
- Full test suite passes (or a clean explanation of why failures are pre-existing
  and tracked) before APPROVE is submitted.

**Does not**: Merge PRs; modify source code directly; auto-resolve conversations;
review PRs it authored (when `$MIMIR_GITHUB_SELF_LOGIN` matches the PR author,
a different reviewer is needed).

When the trigger is `poller:github-activity`, perform the review in the current
turn. Do not call `approve_declassification`: the poller is subject to the taint
gate and cannot approve release of its own inputs. Do not call
`spawn_claude_code` or another spawn tool: a review turn does not delegate into a
coding process. A denied attempt is a working security boundary, not a reason to
request either capability.

## Step-by-step

### 1. Get PR context

```bash
gh pr view <num> --json number,title,body,author,baseRefName,headRefName,headRefOid,state,additions,deletions,changedFiles
```

Note the `headRefOid` (SHA) — you need it if you fetch file content via the
API.

### 2. Get the diff

```bash
gh pr diff <num>
```

The diff is the **authoritative source for what changed**. Read it carefully
before reaching for local file reads.

For PRs whose diff exceeds ~20k lines use the file-list fallback:

```bash
gh pr view <num> --repo jasoncarreira/mimir --json files
```

Filter the returned JSON yourself rather than with `--jq` (see the note below).

> **`--jq` is not available on poller or scheduled turns.** The trusted-service
> shell profiles do not admit it: `gh` evaluates the filter in-process and jq's
> `env` builtin reads the process environment, so the option is a credential
> read rather than an output formatter. Use `--json <fields>` and filter the
> returned JSON yourself. Piped and bracketed filters were already refused on
> those turns anyway, because `|`, `[` and `]` are shell metacharacters.


### 3. Fetch source files only when necessary — and safely

Use local `Read` only when you need extended context beyond what the diff
shows (e.g., the full function surrounding a changed line, a test file
you want to read in full). **Before calling `Read`, ask: "is this file
on the currently-checked-out branch?"** If the PR branch is not checked
out locally, the file may not exist or may show the wrong version.

**Correct alternatives when the local checkout may be stale or on a
different branch:**

```bash
gh pr diff <num> --repo jasoncarreira/mimir
```

On a poller turn, use the diff plus a local `Read` of the checked-out branch.
When the local checkout does not contain the reviewed head, `fetch_url` may read
an exact file at that head using
`https://raw.githubusercontent.com/<owner>/<repo>/<headRefOid>/<path>`. The owner
and repo must be one of the server-configured `GITHUB_REPOS`; arbitrary hosts,
repositories, and path traversal remain denied. `fetch_url` is GET-only and the
returned content remains untrusted. Do not substitute `gh api`: it is outside
the review profile, and pipes or compound shell commands are not admitted.

`fetch_url` returns a cache path under `/attachments/fetch-cache/`. Read that
path with `read_file`; use its `offset` and `limit` for an exact line range, or
use `grep` with `output_mode="content"`, `before_context`, and `after_context`
for bounded context around a match. Always pass the documented absolute virtual
`/attachments/fetch-cache/...` form to `read_file`; although the backend also
resolves relative `attachments/fetch-cache/...` paths, the file-tool schema
requires an absolute path. Do not use `cat`,
`head`, `sed`, `awk`, `python`, or `jq` to slice fetched content: those
shell forms are not the bounded file-read interface and the observed slicing
commands are refused; `awk`/`python` can execute code and `jq` can inspect the
process environment, so widening their admitted forms is not a substitute.
Never replace `fetch_url` with `curl`; direct `curl` to `api.github.com` would
bypass the repository-bound egress adapter and redirect re-check, so it remains
refused.


**If a local `Read` returns "file does not exist":** do NOT bail. Log it
in your notes ("couldn't read <file> locally — reviewing from diff only")
and continue. The diff has the change; local context is supplemental.

This is the most common cause of review turns that draft but never submit.
**A file-not-found error is never a reason to stop before submitting.**

### 4. Draft the review

Write a review that covers:

- **Verdict** — approve, request changes, or comment (see submission flags
  below). Choose based on correctness + blocking issues, not tone.
- **Summary** — 2-4 sentence overview of what the PR does and whether the
  approach is sound.
- **Specific observations** — grouped by file or concern. Use the `> quote`
  convention to anchor suggestions to specific lines.
- **Test coverage note** — are the tests adequate? Are they testing
  the right behavior?
- **Non-blocking suggestions** — style, naming, minor structural notes.
  Label them explicitly as non-blocking so they don't hold up merge.

### 5. Submit — MANDATORY last step

Reserve budget for submission. Once you know the verdict, stop optional
exploration and submit before doing long validation, repeated polling, or
extra context reads. If the tool-call counter is visibly high (around 90+
on a 120-call budget), keep roughly 10-15 calls for the required side
effect (`gh pr review`), submission verification, and wrap-up. A review
with perfect extra evidence that never reaches GitHub is worse than a
bounded review that lands with an honest validation note.

Write the body to a file under the agent scratch root first, then submit it with
`--body-file`. On a poller turn this is the **only** form that works: the profile
execs one argv with `shell=False`, so a heredoc, a `$(...)` substitution, and an
inline multi-line `--body` are all refused — the body is read from the file
during authorization and never re-opened at execution.

```bash
# 1. Write the body (use the Write tool, or a single redirect-free command)
#    to <scratch>/pr-<num>-review.md

# 2. Submit exactly one of these, as a single command:
gh pr review <num> --repo jasoncarreira/mimir --approve         --body-file <scratch>/pr-<num>-review.md
gh pr review <num> --repo jasoncarreira/mimir --request-changes --body-file <scratch>/pr-<num>-review.md
gh pr review <num> --repo jasoncarreira/mimir --comment         --body-file <scratch>/pr-<num>-review.md
```

The body file must resolve beneath the agent scratch root, be a regular file
reached without traversing a symlink, and be at most 64 KiB.

**Do not use a heredoc, `$(...)`, or an inline multi-line `--body`.** All three
are refused before execution on a poller turn, and a refused submission is how a
completed review silently fails to reach GitHub. Write the body to the scratch
file and pass `--body-file`: because the file is read during authorization, `$`,
backticks and multi-line text need no escaping at all.

### 6. Confirm submission

After the `gh pr review` call completes:

```bash
gh pr view <num> --repo jasoncarreira/mimir --json reviews
```

Read the last entry of `reviews` from the returned JSON.

Verify your login (`mimir-carreira`) appears in the reviews list.

### 7. Operator ping (non-mimir-carreira PRs)

For PRs authored by anyone other than `mimir-carreira`, fulfill commitment
`c-5494103062`: send a message to the operator channel noting the PR number
and your verdict.

---

## Non-negotiable rules

1. **Always call `gh pr review` before ending the turn.** If the turn ends
   with a drafted review in the output and no `gh pr review` call in the
   tool sequence, the review was not submitted.

2. **A failing tool call mid-review does not abort submission.** Recover:
   note the failure in the review body, then call `gh pr review` anyway
   with what you have.

3. **Budget exhaustion is not allowed to eat the submission step.** After
   the verdict is known, `gh pr review` plus confirmation outranks optional
   extra reads, broad test runs, or repeated async-job polling. When near
   the tool-call ceiling, submit first with the evidence already gathered.

4. **Never use only turn output as the delivery mechanism.** The operator
   reads GitHub, not `turns.jsonl`.

---

## Poller-triggered reviews

When this skill fires from `poller:github-activity`, the trigger includes
the PR number and repo. There is no human watching the turn output — the
only way the review reaches the operator (and other contributors) is via
`gh pr review`. Apply the same steps above; the poller context changes
nothing about the submission requirement.

---

## Common failure modes

| Symptom | Fix |
|---|---|
| `Read(tests/foo.py)` -> "file does not exist" | Skip Read, continue from diff, still submit |
| `gh pr diff <num>` -> HTTP 406 "diff too large" | Use `gh pr view <num> --repo <owner>/<repo> --json files` (`gh api` is not admitted) |
| `gh pr review` exits non-zero | Check `--help`; verify PR is open; retry once |
| Review body contains backticks / `${}` | Nothing special — `--body-file` reads the file directly, so no shell expansion occurs |
