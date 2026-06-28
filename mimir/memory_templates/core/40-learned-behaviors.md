<!-- desc: behaviors learned through reflection; promoted via core-memory PR -->
# Learned Behaviors

Durable behaviors the agent has learned — a recurring approach that
worked, a failure mode worth avoiding, a heuristic that emerged
across several sessions. This is core memory, so it is READ-ONLY at
runtime: reflection PROPOSES additions here (from
memory/learnings-pending.md) via protected-surface proposal PRs, and they
land only when the operator merges the core-memory PR — nothing
writes this file directly during a turn.

Format per entry:

```
## YYYY-MM-DD — short title
What I noticed: ...
What works: ...
Trigger: <when this applies>
```

## 2026-05-11 — frame-check before design work
What I noticed: Design and implementation requests often smuggle a
premise: that the requested feature belongs in the target subsystem, or
that the named problem is the right problem to solve. Answering inside
that frame can produce polished wrong-direction work.
What works: Run the `core/05-non-goals.md` frame check out loud before
committing to the design: "Before I implement: is X the right thing?
Does Y actually want X?" Then either confirm and proceed, or surface the
doubt before spending effort. This is the procedural counterpart to the
non-goal; it makes frame-checking an action, not just a value.
Trigger: Any request shaped like "how should we implement X", "let's add
X to subsystem Y", "evaluate/review/decide on X", or any design task
where a premise is plausible but not obviously true.

## 2026-06-28 — tune pollers through overrides
What I noticed: Editing an installed skill's `pollers.json` for local
deployment tuning creates skill-source drift and can be lost on skill updates.
What works: Put per-deployment poller tuning in
`<home>/pollers-overrides.yaml`, not in the skill's `pollers.json`.
Overridable fields are `cron`, `priority`, `env`, `pass_env`,
`batch_size`, `recover_failed_turns`, and `deliver`.
Trigger: Changing a poller's schedule, priority, environment passthrough,
static env, batch size, failed-turn recovery, or delivery routing for one deployment.
