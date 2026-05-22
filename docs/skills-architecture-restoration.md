# Design — Restoring open-strix-base's skill architecture

**Status:** Proposed
**Date:** 2026-05-22
**Audience:** mimir maintainers, mimirbot, muninn
**Related:** `docs/skill-as-tool-architecture.md` (PR #263), PR #264 (subagent compilation), open-strix-base reference

## TL;DR

mimir was forked from open-strix-base. During the deepagents migration, several architectural pieces from open-strix-base were dropped. Today's pain — the threadborn confabulation, the scheduled-task / discoverable-skill conflation, the gitignored skill content — all trace back to those dropped pieces.

This doc proposes restoring **three patterns from open-strix-base**, sequenced as separate PRs:

1. **Skills location**: `<home>/.claude/skills/` → `<home>/skills/`
2. **SkillsMiddleware wiring**: replace mimir's custom `_assemble_skill_block` with the framework primitive
3. **CompositeBackend with read-only built-ins**: route bundled skills to a separate read-only backend

PR #264 already brought back open-strix-base's subagent dispatch via `task` tool (mimir was missing that too). The three patterns above complete the restoration. Per-PR scope keeps each change reviewable.

## Why this is a regression, not a new design

Quoting `open-strix-base/open_strix/app.py:475-510`:

```python
skills_sources: list[str] = []
if self.layout.skills_dir.exists():
    skills_sources.append("/skills")
skills_sources.append(BUILTIN_SKILLS_ROUTE.rstrip("/"))
self._log_loaded_skills(skills_sources)
# … create_deep_agent(skills=skills_sources, …)
```

open-strix-base already does what this design proposes. mimir's divergence was a deepagents-migration shortcut that the codebase has been carrying since. None of the patterns below are new — they're already proven in production via open-strix-base.

## The three patterns

### Pattern 1 — Skills location at `<home>/skills/`

**Current mimir state:**
- Skills live at `<home>/.claude/skills/<name>/`
- Deployment gitignore allowlists don't cover `.claude/skills/`, so operator-curated skill content (scripts, references, configs) can't be tracked
- 5 muninn-specific skill workflows existed only as ephemeral on-disk content; one bad cleanup wipes them
- Bundled skills get seeded into `.claude/skills/` from mimir source on first run

**Target state:**
- Skills live at `<home>/skills/<name>/`
- Deployment `.gitignore` files allowlist `skills/**`
- Operator skill content is tracked; supporting scripts/references persist
- Bundled skills seed to `<home>/skills/` on first run
- Legacy `<home>/.claude/skills/` migrated to new location on first startup with new code

**Migration mechanics:**
- One-shot `migrate_legacy_skills_dir(home)` in setup path
- Moves any existing `<home>/.claude/skills/<name>/` to `<home>/skills/<name>/` if the new path is empty
- Idempotent (subsequent runs see the source dir gone, no-op)
- Bundled-skill seeding happens after migration, so post-migration state is uniform

**Files touched** (10 identified during the in-flight WIP):
- `mimir/skill_defs.py` — `home_skills_dir()` constant + helper, `migrate_legacy_skills_dir()`, updated `seed_skills` + `installed_skill_names`
- `mimir/subagent_compiler.py` — default `skills_subdir` from `.claude/skills` → `skills`
- `mimir/skill_outcomes.py` — regex matches new path AND legacy path (don't lose historical telemetry)
- `mimir/agent.py` — `_assemble_skill_block` reads new path via helper
- `mimir/server.py` — calls `migrate_legacy_skills_dir` before `seed_skills`; pollers loader uses new path
- `mimir/scaffold_docker.py` — `collect_fragments` reads new path
- `mimir/pollers.py` — poller infrastructure reads new path
- `mimir/cli.py` — info / setup commands reference new path
- `mimir/skill_install.py` — install target uses new path
- `mimir/scheduler.py` — comment / docstring updates

**WIP state**: `feat/skills-dir-relocate` branch has ~half the changes committed. Resume by completing scaffold_docker, cli, skill_install, pollers; add tests.

### Pattern 2 — SkillsMiddleware wiring

**Current mimir state:**
- `_assemble_skill_block()` (`agent.py:1660`) reads skill catalog from disk and renders a custom block into the system prompt
- `create_deep_agent()` is called without `skills=` kwarg → `SkillsMiddleware` is never instantiated
- Custom code paths for skill catalog, skill telemetry (`render_skill_telemetry`), and skill operator-pin support

**Target state:**
- `create_deep_agent(skills=[<home>/skills/<name>/])` registers `SkillsMiddleware`
- Framework renders the catalog block into the system prompt
- Progressive disclosure (metadata first, full SKILL.md on demand) handled by the middleware
- mimir-specific add-ons (per-skill telemetry counts, pin/hide) layer on top via a thin custom middleware OR a small templated postprocess

**Open-strix-base reference:**
- `skills_sources=["/skills", BUILTIN_SKILLS_ROUTE]` passed to `create_deep_agent`
- The middleware renders skill metadata in the system prompt; the agent uses `read_file` on the SKILL.md path the middleware exposes

**Why split from Pattern 1:** SkillsMiddleware depends on the skill source PATH (Pattern 1). Doing both at once entangles two different concerns (location move, rendering change). Split lets each be reviewed for what it actually changes.

**Files touched:**
- `mimir/agent.py` — pass `skills=` to `create_deep_agent`; thin or drop `_assemble_skill_block`
- `mimir/prompts.py` — the system-prompt assembler no longer composes the skill block (or composes a different shape)
- `mimir/skill_outcomes.py` — the inline-load detection regex still matches; what changes is which skills appear in the catalog
- mimir's telemetry rendering — needs to layer on top of SkillsMiddleware's output

**Open question for Pattern 2:**
- mimir's `_assemble_skill_block` does three jobs: catalog rendering, per-skill success-rate telemetry (`render_skill_telemetry`), and operator pin/hide. SkillsMiddleware does (1) cleanly but doesn't do (2) or (3). The path forward: SkillsMiddleware for catalog; thin custom middleware OR system-prompt postprocess for telemetry + pin.

### Pattern 3 — CompositeBackend with read-only built-ins

**Current mimir state:**
- `WriteGuardBackend` treats all paths under `writable_dirs` as writable
- Bundled skills seeded into `<home>/skills/` are operator-writable — an agent (or operator typo) could mutate them in-place, and the change persists until next re-seed
- No clear "shipped" vs "operator-curated" boundary

**Target state:**
- A `CompositeBackend` routes built-in skills (from the mimir bundle) to a read-only backend
- Operator-installed/customized skills under `<home>/skills/` are writable
- Bundled-skill modifications fail at the backend layer with a clear error

**Open-strix-base reference:**
```python
builtin_backend = build_builtin_skills_backend(
    root_dir=self.home / BUILTIN_HOME_DIRNAME,
)
backend = CompositeBackend(
    default=mutable_backend,
    routes={BUILTIN_SKILLS_ROUTE: builtin_backend},
)
```

**Why split from Patterns 1 & 2:** Pattern 3 is an isolation/safety concern; it doesn't change the agent's behavior in the success case. Operators get a clearer mental model ("bundled skills are read-only") and an extra safety net ("agent can't corrupt the bundle"). But it's invisible until something tries to write — orthogonal to the location and rendering changes.

**Files touched:**
- `mimir/readonly_backend.py` — new read-only backend variant for built-ins
- `mimir/agent.py` — backend wiring switches to `CompositeBackend`
- mimir's seeding logic — bundled skills routed via the built-ins path; operator skills stay on the writable path

**Open question for Pattern 3:**
- Do operators customize bundled skills today (edit `<home>/skills/<bundled-name>/SKILL.md`)? If yes, read-only routing breaks their workflow. If no, this is purely additive safety. Worth checking muninn + mimirbot homes for evidence of bundled-skill edits.

## Sequencing

```
PR A (Pattern 1)  →  PR B (Pattern 2)  →  PR C (Pattern 3)
location move        SkillsMiddleware       CompositeBackend
                     wired                  for built-ins
```

Each PR is reviewable on its own. PR B depends on PR A landing (different file locations). PR C depends on PR A (knows where the bundled skills are routed from) but is independent of PR B.

**Deployment story for muninn / mimirbot:**
- After PR A: restart picks up new code; `migrate_legacy_skills_dir` runs transparently; operator updates `.gitignore` to allowlist `skills/**`
- After PR B: catalog rendering switches to SkillsMiddleware; visible behavior should be identical (or close to it)
- After PR C: bundled-skill write attempts now fail; surface to operator if any agent automation was previously mutating bundled skills

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Existing deployments' operator-customized skills under `.claude/skills/` get lost | `migrate_legacy_skills_dir` moves them transparently; idempotent; logs each move |
| skill_outcomes telemetry gap during migration window (regex still pointed at old path) | PR A's regex matches BOTH `.claude/skills/<name>/SKILL.md` AND `skills/<name>/SKILL.md` — telemetry continuous through the transition |
| SkillsMiddleware renders catalog differently than current `_assemble_skill_block` | Snapshot-compare the rendered system prompt before/after PR B; verify shape change is acceptable |
| Read-only built-ins break an agent workflow that mutates a bundled skill | PR C audit step: grep turns.jsonl for any historical write to a bundled skill path; if any, decide whether to grant exception or fix the workflow |

## Open questions for the design — operator decisions to make

1. **Pattern 2 add-ons.** SkillsMiddleware handles catalog rendering; what's the right shape for layering mimir's telemetry (`success/failure` counters) and pin/hide on top? Three options: thin custom middleware in front of SkillsMiddleware; postprocess on the rendered prompt; drop pin/hide if it's underused.

2. **Pattern 3 boundary.** Where exactly is the "bundled" boundary? Just `mimir/skills/*` from the source repo? Or also any skills the operator declares as "shipped" via config? Most likely the former (matches open-strix-base), but worth confirming.

3. **Poller skills' fate.** Pollers currently live at `<home>/.claude/skills/<name>/` (e.g. `gmail-poller`). They have `pollers.json` manifests alongside SKILL.md. After Pattern 1 they move to `<home>/skills/<name>/`. Do they ALSO benefit from SkillsMiddleware exposure (they're discoverable for the agent to learn about pollers) or are they invisible (operator-invoked, not model-discoverable)? Per the design in `docs/skill-as-tool-architecture.md`, pollers' SKILL.md is documentation for the agent to read when working ON the poller, so they probably belong in the discoverable catalog. SkillsMiddleware exposes them; the poller infrastructure runs them — two separate concerns, both real.

4. **Migration window for deployments.** When PR A lands, the migration helper runs at next restart. If we land PR B before muninn/mimirbot restart, they'd be on PR B's code but PR A's data layout never migrated. Are we comfortable with that, or do we want a clean "deploy PR A first, then PR B" sequence with verification in between?

## Recommended next steps

1. **Confirm this design.** Read with mimirbot; if anything in the migration / sequencing / open questions hits a concern, surface before code lands.

2. **Land PR A** (Pattern 1, in-progress on `feat/skills-dir-relocate`). Includes tests + migration helper. Deploy to muninn first; verify behavior; deploy to mimirbot.

3. **Land PR B** (Pattern 2, SkillsMiddleware). Before-and-after snapshot of system-prompt shape; pin telemetry approach.

4. **Land PR C** (Pattern 3, CompositeBackend). Audit deployments first to confirm no agent workflows write bundled skills.

5. **Retire the WIP `feat/skills-dir-relocate` branch** once PR A is fresh from this plan; don't merge directly from the WIP since it predates the plan refinements.
