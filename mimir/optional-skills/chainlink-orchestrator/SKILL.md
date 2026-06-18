---
name: chainlink-orchestrator
description: "The two model-touching halves of Worklink, shipped as one opt-in skill. (1) Planner: decompose a parent Chainlink issue into testable worklink:ready leaf subissues (mutates Chainlink only, never executes). (2) Ready-queue poller: discovers worklink:ready leaves and dispatches them by invoking `mimir worklink run` as a detached subprocess, up to the concurrent-claim cap — it never reimplements claim/evidence/transition. Opt-in (mimirbot yes, muninn no): `mimir skills install chainlink-orchestrator`, then set the env below to enable autonomous dispatch."
env:
  required:
    - name: WORKLINK_REPO
      description: "Absolute path to the git repo the backend works in (e.g. /path/to/your/repo). The ready-queue poller skips dispatch until this is set; the planner half does not need it."
      example: "/path/to/your/repo"
  optional:
    - name: WORKLINK_RUN_BIN
      description: "Command (shlex-split) the poller invokes for dispatch. Default `mimir`; set to `uv run mimir` or an absolute venv path if bare `mimir` isn't on PATH."
      example: "uv run mimir"
    - name: WORKLINK_MAX_CONCURRENT
      description: "Legacy fallback for total concurrent Worklink claims when worklink.yaml defaults.max_concurrent is absent. Default 2."
      example: "2"
    - name: MIMIR_WORKLINK_REAPER_CRON
      description: "Cron for the core TTL reaper that recovers stale claims (set in the agent env, not here). Empty = reaper disabled."
      example: "*/30 * * * *"
---

<!-- desc: Planner workflow for decomposing parent Chainlink issues into Worklink-ready leaf issues with a single executor-validation template. -->

# Chainlink Orchestrator

This skill is the planning half of Worklink. It turns a vague or multi-step
Chainlink parent into ready leaf issues that the deterministic Worklink executor
can safely claim. The planner is allowed to mutate Chainlink issue structure;
it is **not** allowed to execute the implementation.

## Contract

**Trigger:** You need to prepare a parent issue for Worklink execution, especially
when the parent is too broad for one backend run or lacks testable acceptance
criteria.

**Requires:** Run from the Chainlink repo (your agent home / `$MIMIR_HOME` for
operator-tracked issues). Read the parent issue and relevant spec/doc first. Use the bundled
planner prompt at `mimir/prompt_templates/decompose.md` as the canonical leaf
template.

**Guarantees:**
- Parent work is split into reviewable leaf subissues.
- Each Worklink-ready leaf has the exact template the executor validates:
  `Acceptance criteria`, checklist items, `Review criteria`, and `Worklink notes`.
- Dependency edges are represented with `chainlink issue block <ID-that-is-blocked> <BLOCKER>`; the blocked issue id comes first, then the blocker.
- Only leaves with independently testable criteria receive the `worklink:ready`
  label.

**Does not:** Implement code, run Worklink executor jobs, close issues, open PRs,
or label vague placeholders as ready.

## Planner flow

1. Read the parent: `chainlink issue show <parent-id>`.
2. Read the design/spec named by the parent, if any.
3. Identify the smallest reviewable leaves. Each leaf should have one coherent
   change, an observable outcome, and a focused validation command/evidence.
4. Create each leaf with `chainlink issue subissue <parent-id> --description "$DESC"`.
5. Add dependency edges with `chainlink issue block <ID-that-is-blocked> <BLOCKER>` (blocked issue first, blocker second; do not transpose them).
6. Label only executable leaves with `worklink:ready`.
7. Leave a parent comment summarizing leaf ids, dependency edges, ready leaves,
   and not-ready leaves.

## Required leaf template

Use this exact structure. The executor refuses new issues that do not carry it. The canonical copy is `mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN`; the planner prompt renders that constant and tests assert this skill stays in sync with it.

```markdown
Acceptance criteria:
- [ ] <observable, testable outcome>
- [ ] <focused validation command or evidence requirement>

Review criteria:
- <what a reviewer/operator should verify before approval>

Worklink notes:
- Scope: <files/subsystems expected to change, or "docs only">
- Out of scope: <nearby work not included in this leaf>
- Suggested test command: <command the executor should run>
```

## When not to decompose

Do not use Worklink planning for:
- one-turn conversational answers,
- issues whose success criteria are still a product decision,
- operator-gated work where approval is needed before any leaf is executable,
- tasks that require secrets, credentials, or off-platform side effects the
  executor cannot observe,
- broad research questions without a concrete artifact and validation command.

For those, leave a normal Chainlink comment explaining the gate or create a
non-ready planning subissue without `worklink:ready`.

## Shell-safe comment/description pattern

For multi-line descriptions, write the body to a temp file and use command
substitution to avoid shell quoting corruption:

```bash
cat > /tmp/worklink-leaf.md <<'DESC'
Acceptance criteria:
- [ ] ...

Review criteria:
- ...

Worklink notes:
- Scope: ...
- Out of scope: ...
- Suggested test command: ...
DESC
chainlink issue subissue <parent-id> --description "$(cat /tmp/worklink-leaf.md)" "<title>"
```

If adding `worklink:ready` fails because the label does not exist, create or use
whatever project label convention the parent already established; do not proceed
silently with an unlabeled executable leaf.

## Ready-queue poller (slice 3 — autonomous dispatch)

`pollers.json` + `poller.py` in this skill are the autonomous execution half.
Once installed and configured (see frontmatter env), the scheduler runs the
`worklink-ready-queue` poller on its cron (default every 10 min). Each fire:

1. Lists open `worklink:ready` leaves, intersects them with `chainlink issue ready`
   actionability, and counts active Chainlink locks. A pre-labeled leaf blocked by
   open dependencies stays untouched until Chainlink reports it ready; once its
   blocker closes it is eligible on the next poller fire.
2. Computes free slots from `worklink.yaml` `defaults.max_concurrent` minus active claims. Default cap is 2; `WORKLINK_MAX_CONCURRENT` is only a legacy fallback when the YAML key is absent.
3. Launches `mimir worklink run <id>` **detached** for up to that many actionable leaves,
   then returns immediately (a run can take minutes; the poller's own 60s budget
   would otherwise kill it). The detached run does the claim/evidence/transition
   in the deterministic core executor.

The detached run inherits the **poller's** env, so `pass_env` must carry anything
the run needs — notably **`GITHUB_TOKEN`**: the core executor's `_open_pr`
(`gh pr create`) runs on the controller side in this subprocess (not in the
worker), so without the token `gh` can't authenticate and review-ready runs fail
at PR creation → thrash to `worklink:blocked`. (Manual `bash -lc` dispatch hides
this — it inherits the container's token.)

Safety properties (do not bypass these in the poller):
- **Per-issue exclusivity** is guaranteed by `chainlink locks claim` *inside*
  the run, not by the poller — a duplicate launch for the same id simply fails
  to claim and exits.
- **Shedding under pressure**: the poller declares `priority: normal` in
  `pollers.json`, so the scheduler's arbiter suppresses the whole fire under
  TIGHT (and worse). The in-turn `worklink_run` tool consults the arbiter
  directly; the operator CLI (`mimir worklink run`) bypasses both — it always
  proceeds.
- **Stale recovery**: a worker that dies leaves a claim; the core TTL reaper
  (`MIMIR_WORKLINK_REAPER_CRON`) steals it back to `worklink:ready` (or
  `worklink:blocked` once attempts are spent).

The poller never decides *what* is implementable — only the planner half (and
the `worklink:ready` label it applies) does. The poller dispatches only the
subset that is both marked ready and currently unblocked by Chainlink, so it is
safe to apply `worklink:ready` ahead of dependency availability.
