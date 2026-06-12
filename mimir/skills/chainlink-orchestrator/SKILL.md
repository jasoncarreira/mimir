---
name: chainlink-orchestrator
description: Plan Worklink-ready Chainlink issue trees. Use when decomposing a parent issue into testable Worklink leaf subissues with acceptance criteria, dependency edges, and ready labels; the planner mutates Chainlink only and never executes implementation work.
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

**Requires:** Run from the Chainlink repo (`/mimir-home` for operator-tracked
issues). Read the parent issue and relevant spec/doc first. Use the bundled
planner prompt at `mimir/prompt_templates/decompose.md` as the canonical leaf
template.

**Guarantees:**
- Parent work is split into reviewable leaf subissues.
- Each Worklink-ready leaf has the exact template the executor validates:
  `Acceptance criteria`, checklist items, `Review criteria`, and `Worklink notes`.
- Dependency edges are represented with `chainlink issue block <blocker> <blocked>`.
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
5. Add dependency edges with `chainlink issue block <blocker> <blocked>`.
6. Label only executable leaves with `worklink:ready`.
7. Leave a parent comment summarizing leaf ids, dependency edges, ready leaves,
   and not-ready leaves.

## Required leaf template

Use this exact structure. The executor refuses issues that do not carry it. The canonical copy is `mimir.worklink.planning.LEAF_TEMPLATE_MARKDOWN`; tests assert this skill and the planner prompt stay in sync with that constant.

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
