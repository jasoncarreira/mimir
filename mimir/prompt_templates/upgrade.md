---
name: upgrade
description: Version-triggered reconciliation of bundled prompts/core defaults. Runs when startup opens an upgrade-lane proposal worktree after shipped defaults changed.
---

# Upgrade defaults reconciliation

Startup opened an upgrade-lane proposal for mimir-agent version `{version}`.

Proposal branch: `{branch}`
Proposal worktree: `{worktree}`
Startup action: `{action}`
Conflict markers present: `{conflicts}`

Your job:

1. Read the changed files under `{worktree}/memory/core/` and `{worktree}/prompts/`.
2. Review the staged diff in the proposal worktree (`git diff --cached` from `{worktree}`).
3. If any file contains conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`), resolve them deliberately:
   - preserve local/operator customizations that are still load-bearing;
   - incorporate shipped-default changes that update policy, workflows, or safety boundaries;
   - remove the conflict markers before submission.
4. If the clean merge is already acceptable, do not churn the files just to touch them.
5. Submit the proposal with `submit_proposal(title, rationale, lane='upgrade')` so the operator gets a PR. Approval is still the merge.

Suggested title: `Upgrade mimir defaults to {version}`

Suggested rationale: `Reconciles bundled mimir-agent {version} prompt/core-memory defaults with this home's existing customizations. Approval = merge; live files update only after the proposal PR merges.`

Do not edit live `memory/core/*` or `prompts/*` directly. Work only in the proposal worktree above.
