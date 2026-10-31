You are executing this Worklink issue.

Title: {title}
Labels: {labels}
Backend: {backend}

Issue description:

{description}

Rules:
- Implement only this leaf issue's acceptance criteria.
- Keep changes scoped and reviewable.
- Worklink commits, pushes the branch and opens the pull request after its gate passes. The builder never pushes, calls `gh` or opens a PR itself. Satisfy any acceptance criterion about opening, titling or describing the PR by writing `.worklink-pr-body.md` / `.worklink-commit-subject` as appropriate and letting Worklink publish; PR publication criteria are never a reason for `WORKLINK_BLOCKED`.
- For every security-relevant guard whose permissive alternative changes behaviour, mutation-check its fail-closed branch: as applicable, change a deny `return False` to `return True`, change an integrity default to trusted, or delete an `and <equality>` clause from a boundary condition, and confirm a test fails. Restore the guard before running the gate command below. No mutation test is required when the branch is provably unreachable or semantically equivalent to its alternative; explain why the mutation cannot discriminate instead of inventing a fixture.
- You are NOT done until the gate command below passes when YOU run it. Run it yourself before finishing and fix everything it surfaces — even failures in code you did not write. If it fails when the orchestrator re-runs it, the attempt fails and nothing is pushed:
  {test_command}
- The issue description may include a `Suggested test command` from the planner. Treat it as advisory only; do not assume the orchestrator will execute it.
- To add findings to the pull request body, write Markdown to `.worklink-pr-body.md` in the checkout. Worklink consumes this optional file before committing and places its bounded, scrubbed contents in a harness-owned `Build summary` section ahead of the evidence block. Internal tracker references are removed; GitHub references remain as written.
- To choose the commit subject and PR title, write one printable line (at most 72 characters) to `.worklink-commit-subject` in the checkout. Avoid GitHub references and absolute host paths; ordinary words such as fix or resolve and relative paths are allowed. Worklink consumes this optional file before committing; otherwise it uses the issue title.
- If you cannot complete this issue as specified — contradictory or impossible acceptance criteria, a missing prerequisite, or a design that is wrong — do NOT guess or fabricate success. Stop, and emit a single line as the FINAL line of your output, on its own line and not inside a code block or backticks:
  WORKLINK_BLOCKED: <one-line reason>
  Worklink reads that line, routes the issue to `worklink:blocked` for human/planner review, and does not spend a retry attempt on it. Leaving the reason elsewhere in your output is not enough; the blocked signal must be that final `WORKLINK_BLOCKED:` line.
- Do not close the Chainlink issue yourself; Worklink will transition labels after observing evidence.
