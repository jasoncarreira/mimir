---
name: review
description: Review a pull request and POST the review to GitHub via `gh pr review`. Use when asked to review a PR, or when the github-activity poller fires a PR-opened / PR-review-requested event. Always submits after drafting — a review that stays in turn output and never reaches GitHub is a non-review.
---

# PR Review

Read the diff, draft a verdict, and **submit it via `gh pr review`**. The
submission step is not optional — it is the definition of "done" for this
skill.

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
gh api repos/jasoncarreira/mimir/pulls/<num>/files --paginate \
  --jq '.[] | "\(.status) \(.filename)"'
```

### 3. Fetch source files only when necessary — and safely

Use local `Read` only when you need extended context beyond what the diff
shows (e.g., the full function surrounding a changed line, a test file
you want to read in full). **Before calling `Read`, ask: "is this file
on the currently-checked-out branch?"** If the PR branch is not checked
out locally, the file may not exist or may show the wrong version.

**Correct alternatives when the local checkout may be stale or on a
different branch:**

```bash
# Fetch a file from the PR's HEAD SHA via API
gh api "repos/jasoncarreira/mimir/contents/<path>?ref=<headRefOid>" \
  --jq '.content' | base64 -d

# Or fetch the raw blob
gh api "repos/jasoncarreira/mimir/git/blobs/<blob_sha>" --jq '.content' \
  | base64 -d
```

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

```bash
# Approve
gh pr review <num> --approve --body "$(cat <<'EOF'
## Summary
...

## Observations
...
EOF
)"

# Request changes
gh pr review <num> --request-changes --body "$(cat <<'EOF'
## Summary
...

## Blocking issues
...
EOF
)"

# Comment only (no verdict, for questions / early feedback)
gh pr review <num> --comment --body "$(cat <<'EOF'
...
EOF
)"
```

Use a heredoc (`<<'EOF' ... EOF`) to pass the body — inline quoting breaks
on `$`, backticks, and multi-line text.

### 6. Confirm submission

After the `gh pr review` call completes:

```bash
gh pr view <num> --json reviews --jq '.reviews[-1] | {state, author: .author.login, submitted: .submittedAt}'
```

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

3. **Never use only turn output as the delivery mechanism.** The operator
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
| `gh pr diff <num>` -> HTTP 406 "diff too large" | Fall back to `gh api .../pulls/<num>/files` for file list |
| `gh pr review` exits non-zero | Check `--help`; verify PR is open; retry once |
| Review body contains backticks / `${}` | Use `<<'EOF'` heredoc (single-quote prevents expansion) |
