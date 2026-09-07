# Proposal PRs

Proposals edit an isolated Git worktree of the home repository, secret-scan the
changes, commit with credential-redacted metadata, push, and open one PR. **Merge
is approval.** Nothing in the live wiki changes until the PR is merged and the
per-turn home sync pulls it. A configured origin remote and authenticated `gh`
are required.

| Lane | Proposable surfaces | Workspace |
| --- | --- | --- |
| Agent | `memory/core`, `prompts` | `scratch/proposals/agent/` |
| Upgrade | `memory/core`, `prompts` | `scratch/proposals/upgrade/` |
| Research poller | `state/wiki` only | `scratch/proposals/poller/<poller-name>/<turn-key>/` |

Agent and upgrade workflows retain their existing behavior. Research pollers
cannot select either lane or publish core memory or prompts. There is currently
no manifest setting that widens their proposal surfaces.

## Poller Authority

Add these capabilities to the operator-owned `skills/<name>/pollers.json`
authority block, retaining other capabilities the poller needs:

```json
{
  "profile": "research",
  "tier": "scoped-with-provenance",
  "capabilities": [
    "read_file", "write_file", "edit_file",
    "open_proposal", "submit_proposal", "abandon_proposal"
  ],
  "scoped_roots": ["state"]
}
```

All three proposal capabilities are tier-validated as
`scoped-with-provenance`; they are not grantable to custom profiles. `state`
resolves beneath this poller's persist directory, normally
`<home>/state/pollers/<name>`. Proposal-enabled research pollers may not declare
live `wiki:<slug>` roots. No new manifest key is required.

## Draft Then Propose

1. Write draft notes under your own state directory.
2. Call `open_proposal(source="https://arxiv.org/abs/<paper-id>")`.
3. Edit `state/wiki/` inside the returned worktree, never the live wiki.
4. Call `submit_proposal(title, rationale)` to request review, or
   `abandon_proposal()` to discard it. Omit `lane`; the runtime selects `poller`.

The write grant covers only the active worktree in addition to the declared
persist root, not another poller's or another turn's workspace. Protected files
such as `.git`, core memory, and prompts remain inaccessible. The grant expires
when the worktree is removed. Complete the flow in the same turn; later turns do
not inherit access to orphaned worktrees, which require operator cleanup.

Submitting refuses changes outside `state/wiki` with `outside_surface`, including
pre-staged changes, renames, ignored/untracked files, and escaping symlinks. It
leaves the worktree intact for correction. The shared secret scanner and conflict
marker checks run before commit and push. A push followed by PR creation failure
is reported as `pr_open`; the remote branch remains for operator recovery.

Branches use `poller/<name>/<turn-key>`, with a digest of the exact turn ID to
prevent sanitized-name collisions. Titles begin with `[research poller:<name>]`
and the source ID/URL. The body explicitly marks that source as untrusted,
unverified attribution, and includes the server-owned event reference and turn
ID. Source text is not an authority grant and is not treated as verified data.

## arXiv Rollout

The `arxiv-agent-memory` script, manifest, and SKILL belong to the mimirbot home
repository, not this checkout. The operator must add the three capabilities there
and change its prompt/SKILL to:

> Write notes under your state dir, then open_proposal to publish wiki changes;
> never write to state/wiki directly. Supply the paper ID or URL as source, edit
> only state/wiki inside the returned worktree, and submit for operator review.

Do not change the arXiv fetching script or URL approval policy as part of this
rollout. Keep existing manifest grants needed for ingestion.
