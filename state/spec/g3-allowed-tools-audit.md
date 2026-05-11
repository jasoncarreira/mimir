# G3 — `allowed-tools:` audit for bundled mimir skills

Source: chainlink #79 (subissue under chainlink #29 GBrain pattern adoption).
Generated 2026-05-11 09:00 UTC heartbeat. The audit walks
`mimir/skills/<name>/SKILL.md` body text and records which tools each
skill explicitly references. This is the docs-only baseline; runtime
enforcement is deferred (see "Enforcement decision" section at end).

## Audit rule

For each skill, list the tools the skill BODY explicitly references or
documents using. Do not aggressively infer broader surfaces — if the
skill is a conceptual menu (circuit-breaker, fallback-chains), the
surface stays narrow.

Tools enumerated using their short name (no `mcp__mimir__` prefix in
frontmatter — the field is for human readability and reviewer drift
detection, not the harness's tool-call surface).

> **Snapshot — not auto-regenerated.** The table below is the
> 2026-05-11 09:00 UTC body-scan baseline. SKILL.md `allowed-tools`
> declarations may have grown since via subsequent PRs (declared
> lists are a superset of body references; the audit walks BODY
> only, so divergence is structurally expected as skills evolve).
> Run a fresh body-scan via the chainlink #79 process before using
> this table as ground-truth for current state. The conformance
> test (`tests/test_skill_conformance.py`) enforces the
> body-vs-declared invariant on PR — that's the live check.

## Per-skill surfaces

| Skill | Tools |
|-------|-------|
| alert | Read, send_message |
| async-tasks | Bash, Read, bash_async, bash_job_output, bash_jobs_list, send_message |
| chainlink | Bash, Read |
| circuit-breaker | Read |
| fallback-chains | Read, saga_query |
| find-skills | Bash, Read |
| five-whys | Bash, Edit, Read, Write |
| github | Bash |
| heartbeat | Bash, Edit, Read, Write, send_message |
| identity-lookup | Bash, Read |
| introspection | Bash, Read |
| journal | Bash, Read |
| long-running-jobs | Bash |
| memory | Edit, Glob, Read, Write, file_search, saga_query, saga_store |
| mermaid-diagrams | Bash, Read, Write, send_message |
| mountaineering | Agent, Read, Write |
| ntfy | Bash, Read |
| onboarding | Edit, Read, Write |
| pollers | Bash, Read, Write, reload_pollers |
| predictions | Bash, Read |
| reflection | Bash, Edit, Glob, Read, Write |
| skill-acquisition | Bash, Read |
| skill-creator | Edit, Read, Write |
| tmux | Bash |
| try-harder | Edit, Glob, Read, Write |
| view-attachment | Bash, Read |
| weather | Bash |
| wiki | Edit, Read, Write, file_search, saga_store |
| world-scanning | Read |

## Judgment calls

- **circuit-breaker, fallback-chains, world-scanning, try-harder**: largely
  conceptual menus. Listed only `Read` plus what each explicitly cites.
  `world-scanning` cross-references the `pollers` skill but doesn't itself
  invoke poller tooling.
- **chainlink**: uses the `chainlink` CLI via `Bash`. Omitted `Edit`/`Write`
  because the CLI itself mutates state; the skill body is shell commands.
- **github**: pure `Bash`-via-`gh`. No `Read`/`Write` documented in the body.
- **mountaineering**: lists `Agent` because the skill explicitly invokes the
  SDK's Task/Agent tool with `subagent_type=climber`. `Bash` not documented.
- **predictions**: uses the `mimir predictions` CLI via `Bash`. No file edits
  documented.
- **heartbeat**: documents memory edits, jq/bash investigations,
  `send_message` for the operator alert channel. Broad surface intentional.
- **memory/wiki**: explicitly call out `file_search` and `saga_store`. Wiki
  body shows `saga_store` but not `saga_query` (the pre-message hook is
  implicit).
- Several skills likely use more than what's documented (e.g. `chainlink`
  for state mutation, `github` for PR creation flows tied to git).
  Held to the "only what the body explicitly references" rule. If runtime
  enforcement is adopted later, surface widening will be detected by the
  conformance test failing on actual-vs-documented gap.

## Enforcement decision (operator-gated, parked for review)

The subissue called out two paths:

**Path 1: Docs-only (this PR).** Field exists in frontmatter. Conformance
test asserts presence + valid list shape. Drift is caught at PR review
time + by the test failing if a skill is added without the field. No
runtime impact — every turn still has access to every tool.
- Pro: cheap, ships in this heartbeat, sets up schema.
- Pro: reviewers can spot when a skill body grows new tool dependencies
  without updating the frontmatter (gradient catches drift).
- Con: skill author can lie / forget — drift only caught at PR time.

**Path 2: Runtime filtering (deferred follow-up).** Mimir harness reads
frontmatter at dispatch and filters tool access per skill. Meaningful
harness change in the skill-call boundary; coordinates with the
find-skills ranker. Would be its own subissue under chainlink #29.
- Pro: hard contract — drift is impossible.
- Con: meaningful design work. Where is the per-skill boundary in mimir?
  Skills today don't run in isolated sub-turns — they're prompt
  inclusions plus tool-call permissions on the parent turn. Filtering
  by skill requires picking a moment when "the skill is active" is
  well-defined.

**Recommendation:** ship Path 1 now. Park Path 2 as a follow-up subissue
that requires operator approval to commit the harness design.

Operator decision needed before pursuing Path 2 (not blocking this PR):
1. Should runtime filtering be pursued at all, given mimir's "every turn
   has every tool" baseline?
2. If yes, what's the skill-active scope — per-turn (one skill at a time,
   selected by find-skills) or layered (multiple skills active, union of
   their tools)?

If the answer to (1) is "no", the field stays docs-only forever, which
is still a net win (drift detection, catalog input for G5).
