---
name: skill-creator
description: Create or update reusable skills for this agent. Use this skill ONLY when the user asks to create a new skill, edit an existing skill, improve a SKILL.md, or capture a repeated workflow as a reusable skill. Do not use this skill for one-off tasks.
success_criteria:
  # The skill's lasting artifact is a Write or Edit to a SKILL.md
  # under the skills/ tree. The "create a new skill" use case
  # produces a Write; "improve a SKILL.md" produces an Edit.
  any_of:
    - tool_call:
        name: Write
        args:
          file_path_glob: "*skills/*/SKILL.md"
    - tool_call:
        name: Edit
        args:
          file_path_glob: "*skills/*/SKILL.md"
---

<!-- desc: How to author a new skill — frontmatter shape (name, description, success_criteria), desc-line, body conventions; cite mimir/skill_md.py as parser-of-record. -->

# skill-creator

Create or update local skills in this agent home repo.

## Phase 0: Should this be a skill?

Before creating a new skill, answer these three questions. If any answer
is "no" or "unclear" — stop and write a reply or a wiki page instead.

1. **Recurring?** Has this workflow appeared ≥2 times, or is it clearly
   going to repeat? One-off workflows are not skills — they bloat the
   catalog and dilute discoverability.

2. **Non-trivial?** Will the body exceed ~20 lines of meaningful steps?
   A three-line procedure belongs in a reply, not a skill file.

3. **Clear trigger?** Can you write a one-line description that
   unambiguously tells the agent *when* to invoke this skill — and when
   NOT to? Fuzzy triggers produce noisy skill catalog entries that get
   invoked on the wrong task.

All three "yes" → proceed. One or more "no" → don't create the skill.

## Where Skills Go

User-editable skills belong in:
- `skills/<skill-name>/SKILL.md`

Example:
- `skills/triage-issues/SKILL.md`

Built-in skills are exposed at:
- `/mimir/skills/<skill-name>/SKILL.md`

Treat built-in skills as read-only.

## Critical Rule: Trigger Description

The YAML frontmatter `description` is the trigger signal. It must make it obvious
when the skill should be used.

Every skill description should include:
- what the skill does
- exact "when to use" triggers
- what it should not be used for

Bad description:
- `Helps with docs.`

Good description:
- `Create and update release notes from git history. Use when the user asks for changelogs, release summaries, or version notes. Do not use for code changes.`

## Frontmatter schema

Every SKILL.md begins with YAML frontmatter delimited by `---` lines. The parser of record is `mimir/skill_md.py` — when in doubt, that file determines what's accepted and rejected. Required and recommended fields:

| Field | Required | Shape | Notes |
|---|---|---|---|
| `name` | yes | string | The skill's short identifier (matches the directory name). |
| `description` | yes | string | Trigger description — what the skill does + exact "when to use" cues + what it should NOT be used for. Single line is safest; if you need length, use plain quoted strings (NOT YAML folded `>` blocks — those have a known parser pitfall, see "Gotchas" below). |
| `success_criteria` | optional | dict | Operator-declared "did the skill's procedure actually run?" test for outcome telemetry. See `mimir/skill_outcomes.py:SkillSuccessCriteria` for the shape (`any_of` list of `tool_call` patterns, with `name` + optional `args` / `args_glob`). Skills with a clear declarative completion signal benefit from declaring this; meta-cognitive skills can omit it. |

## The `<!-- desc: -->` first-body-line convention

The line immediately after the closing `---` of frontmatter should be an HTML comment of the form:

```html
<!-- desc: <one-line description of what's in this file> -->
```

The auto-managed `memory/INDEX.md` (and `state/INDEX.md`) scrape this line to render the file's row in the every-turn-visible index. Without it, the indexer falls back to the first sentence prefixed with `[auto]` — usable but lower-signal for skill authors who want their skill discovered.

**Important scope note**: SKILL.md files are **not** currently walked by the memory indexer. The desc-line in a SKILL.md is a convention for human readers and future tooling — it is not scraped into `memory/INDEX.md` or `state/INDEX.md` today. The live skill discovery path is `memory/skills-catalog.md` (maintained by `mimir skills catalog`, regenerated on index drift). If the memory indexer is extended to walk skill directories in a future release, the desc-line would automatically become the index entry — that's why the convention is enforced now.

Conventions:
- Single line, ≤200 chars. The row appears in the system prompt every turn — keep it dense.
- Agent-facing voice (what the skill *does*), not engineer-facing (which chainlink filed it, what commit shipped it).
- Stable across revisions — if the skill's purpose changes substantially, change the `<!-- desc: -->`; if just the body changed, leave the comment alone.

The conformance test (`test_skill_md_body_starts_with_desc_comment` in `tests/test_skill_conformance.py`) enforces this — a missing `<!-- desc: -->` is a CI failure (chainlink #102). The line is required; the indexer's `[auto]` fallback path is a lint-only safety net, not a production path.

## Why no `allowed-tools` field

Earlier versions of mimir tracked an `allowed-tools` frontmatter field listing the tools each skill's body documented using. It got removed 2026-05-23 because:

1. **deepagents' SkillsMiddleware silently rejected it.** The parser only accepts the space-separated string form (per the Anthropic Agent Skills spec); mimir used the YAML-list form. Every parse logged "Ignoring non-string 'allowed-tools'" at DEBUG level (invisible at INFO) and treated the field as empty.
2. **No runtime enforcement existed.** The original PR #264 spike compiled `allowed-tools`-declaring skills into restricted-tool SubAgents — but the LLMs routed around delegation in production (PR #271), so the SubAgent path was ripped out.

The skill body still describes which tools the procedure uses in prose; that documentation is the supported pattern today. Use `success_criteria` to *measure* whether the canonical tool actually fired.

## Skills that send a message to a specific channel

If your skill calls `send_message`, be deliberate about `channel_id` — getting it wrong fails at runtime, not authoring time.

- **Replying in context? Omit `channel_id`.** It defaults to the current turn's channel (`send_message(text=...)`). This is the right choice for almost every skill — don't name a channel you don't need to.
- **Targeting a *different* channel? The id must be prefix-qualified, not a raw platform id.** The registered bridge dispatches by prefix:
  - `discord-<channel_id>` / `dm-discord-<channel_id>`
  - `slack-<channel_id>` / `dm-slack-<user_id>`
  - `web-<conv_id>`, `bench-<task_id>`

  A bare platform id (e.g. a raw Discord channel number like `123456789012345678`) has **no registered bridge** and raises `UnknownChannelError` (`mimir/channel_registry.py`). The canonical prefix scheme is SPEC §7.2.3.
- **Never hardcode a channel id in the SKILL.md.** Channel ids are deployment-specific — a literal baked into the skill body will work in your head and fail on someone else's deployment. Source them from runtime instead:
  - The operator alert channel is named in the system prompt's *Operator config* section (`MIMIR_OPERATOR_ALERT_CHANNEL`). The `alert` builtin skill is the reference example.
  - Other targets come from the current turn (omit `channel_id`), the identities registry, or operator-set skill config — not a literal in the body.

## Developer-facing sibling docs (`DESIGN.md` and friends)

When a skill has content that's useful to *developers* (impl cross-refs,
architecture notes, migration history) but doesn't help the agent mid-turn,
putting it in SKILL.md costs prompt tokens every time the agent reads the
file. The fix is a **sibling doc** in the same directory.

### Convention

For any skill that has developer-facing supplementary content, add a sibling
file alongside SKILL.md:

```
mimir/skills/<name>/
├── SKILL.md           # agent + operator-facing: prose, frontmatter, usage
└── DESIGN.md          # developer-facing: implementation cross-refs (optional)
```

Other potential siblings that may emerge:
- `ARCHITECTURE.md` — internal mechanism notes too long for SKILL.md prose
- `MIGRATION.md` — how this skill replaced earlier patterns, upgrade paths

### Pointer line in SKILL.md

Add a single blockquote near the relevant section (or in a footer):

```markdown
> Implementation seams (code paths for each tier) are in
> [`DESIGN.md`](DESIGN.md) — developer reference; not loaded by the agent.
```

The pointer is **prose**, not a structural element — it tells a developer
where to look without making the agent load the file. The agent won't
auto-surface DESIGN.md unless it explicitly `Read`s the path.

### What goes in DESIGN.md

Typically: a heading per concept from SKILL.md (tier, step, mechanism) with
a `_→ file.py:function()_` annotation pointing to the implementing code.
The memory skill's `DESIGN.md` (PR #388) is the canonical reference
example — 12 tiers mapped to `core_blocks.py`, `prompts.py`, `searchtools.py`,
etc.

### Per-skill conformance test (recommended)

Each adopter should add a test that pins the invariant across both files.
For the memory skill:
- Test asserts: tier count in SKILL.md ≥ 12, seam annotation count in
  DESIGN.md ≥ 12.
- When one file changes without the other, the test fails loudly.

See `tests/test_skill_defs.py:test_memory_skill_visibility_tiers` for the
pattern.

### When to use

Opt-in, not required. Use when:
- The implementation-seam / architecture notes are long enough that reading
  SKILL.md mid-turn noticeably increases token cost (≥100 lines or ≥10 seam
  annotations).
- The content is clearly developer-facing (code paths, file line numbers,
  design rationale for maintainers) rather than agent-useful context.

For short skills (< 100 lines total), keep all content in SKILL.md.

## Gotchas

- **YAML folded scalars** (`description: >`): the bundled `mimir/skill_md.py` parser handles `description: >` and `description: |` blocks correctly (covered by `test_parse_frontmatter_handles_folded_description`; `mimir/skills/onboarding/SKILL.md` ships using folded form), so mimir's own catalog + INDEX rendering is fine. The pitfall is for *downstream* tooling that uses `yaml.safe_load` directly: if the next key sits at the same indent without a blank-line separator, `yaml.safe_load` can fold it into the description's value. Stick to plain quoted strings if you want maximum portability across tooling that doesn't go through `mimir/skill_md.py`.
- **Stale catalog**: after editing a SKILL.md, run `mimir skills catalog` to regenerate `memory/skills-catalog.md`. The catalog isn't auto-regenerated on file write today.
- **Missing `<!-- desc: -->`**: CI fails (`test_skill_md_body_starts_with_desc_comment`). Add the comment immediately — see step 3 of the Authoring Checklist below.

## Authoring Checklist

1. **Phase 0 gate** — verify Phase 0 criteria above before touching anything.
2. Write frontmatter with `name` and a high-signal `description`. Optionally add a `success_criteria` block if the skill has a clear declarative completion signal (a Bash command pattern, a Write to a specific path, etc.) — see `mimir/skills/weather/SKILL.md` for a minimal example.
3. Add the `<!-- desc: -->` first-body-line comment (agent-facing voice, dense).
4. Add concise execution steps in the SKILL body.
5. Include concrete paths/commands the agent should run. If the skill calls `send_message`, follow "Skills that send a message to a specific channel" above — omit `channel_id` to reply in context, and never hardcode a channel id.
6. Keep scope narrow; split broad domains into multiple skills.
7. Prefer deterministic instructions over generic advice.
8. **Quality gate — read the skill as an outsider.** Before writing tests, re-read the complete SKILL.md as if you were a different agent seeing it for the first time. Ask: Is the trigger unambiguous? Would the procedure produce consistent output across different runs? Are the steps concrete enough to follow without context? Fix gaps now — tests lock in behavior, so verify quality *before* tests encode it.
9. **Optional: sibling doc** — if the skill has ≥100 lines of developer-facing implementation notes (code paths, architecture rationale), move them to `DESIGN.md` and add a pointer blockquote in SKILL.md. See "Developer-facing sibling docs" above.
10. **Test-gate (new skills only)** — For a *new* skill (not an edit to an existing one), write `tests/test_<skill-name>_skill.py` with at minimum one skill-specific assertion before declaring the skill accepted. The assertion should check something the general conformance suite doesn't already cover — e.g., that the SKILL.md body contains the required phases/sections, that the `success_criteria` pattern validates correctly against a sample tool-call, or that a key trigger phrase is present in the description. Then run all three: `pytest tests/test_skill_conformance.py tests/test_skill_catalog.py tests/test_<skill-name>_skill.py`. If any test fails: revise SKILL.md (or the test) and re-run — **do not declare done on a failing suite**. The rewrite-loop is: write → run tests → fail → revise → re-test → accept. The skill is only "registered" once all targets pass green. This is the MUSE-Autoskill quality gate (`state/wiki/concepts/muse-autoskill-skill-lifecycle.md` §Test-gated registration).
11. Run `pytest tests/test_skill_conformance.py tests/test_skill_catalog.py` locally to catch frontmatter drift before pushing.
12. After landing, run `mimir skills catalog` to refresh the catalog file.
