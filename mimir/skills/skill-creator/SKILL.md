---
name: skill-creator
description: Create or update reusable skills for this agent. Use this skill ONLY when the user asks to create a new skill, edit an existing skill, improve a SKILL.md, or capture a repeated workflow as a reusable skill. Do not use this skill for one-off tasks.
allowed-tools:
  - Edit
  - Read
  - Write
---

<!-- desc: How to author a new skill — frontmatter shape, allowed-tools, desc-line, body conventions; cite mimir/skill_md.py as parser-of-record. -->

# skill-creator

Create or update local skills in this agent home repo.

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
| `allowed-tools` | yes | **YAML list** | Tools this skill's body documents using. **List form only** — bullet (one item per line, `- Read`) or inline array (`[Read, Write]`). The scalar form `allowed-tools: Foo` is rejected by the conformance test. Use an explicit empty list `allowed-tools: []` if the skill is pure prose with no tool calls. |

## The `<!-- desc: -->` first-body-line convention

The line immediately after the closing `---` of frontmatter should be an HTML comment of the form:

```html
<!-- desc: <one-line description of what's in this file> -->
```

The auto-managed `memory/INDEX.md` (and `state/INDEX.md`) scrape this line to render the file's row in the every-turn-visible index. Without it, the indexer falls back to the first sentence prefixed with `[auto]` — usable but lower-signal for skill authors who want their skill discovered.

Conventions:
- Single line, ≤200 chars. The row appears in the system prompt every turn — keep it dense.
- Agent-facing voice (what the skill *does*), not engineer-facing (which chainlink filed it, what commit shipped it).
- Stable across revisions — if the skill's purpose changes substantially, change the `<!-- desc: -->`; if just the body changed, leave the comment alone.

The conformance test does NOT currently fail-loud on a missing `<!-- desc: -->`; the indexer falls back gracefully (see `tests/test_skill_conformance.py` for the current coverage shape). Treat it as a soft requirement — landing it improves your skill's discoverability without breaking CI.

## Allowed-tools enforcement (current status)

The `allowed-tools` field is **docs-only today** — `mimir/agent.py` does NOT consume it for runtime sandboxing. The conformance test (`tests/test_skill_conformance.py`) ensures every tool the skill BODY mentions is also declared in the frontmatter list (body-vs-declared audit), so the field is a *true* "what tools does this skill need" map for review purposes. Phase-2 runtime filtering is tracked separately (see `state/spec/g3-allowed-tools-audit.md` for the audit baseline and the Path-2 decision parking).

Do NOT rely on `allowed-tools` as a sandbox; relying on it as documentation is the supported use today.

## Gotchas

- **YAML folded scalars** (`description: >`): if the next key is at the same indent without a blank line, YAML can fold it into the description's value. Stick to plain quoted strings unless you've tested round-trip parsing via `python -c "import yaml; print(yaml.safe_load(open('SKILL.md').read().split('---')[1]))"`.
- **`allowed-tools: Foo`** (scalar) is silently parsed by some YAML readers as `allowed-tools: "Foo"`. The conformance test rejects this — the field is **list-only**. Use `allowed-tools: [Foo]` or the multi-line bullet form.
- **Stale catalog**: after editing a SKILL.md, run `mimir skills catalog` to regenerate `memory/skills-catalog.md`. The catalog isn't auto-regenerated on file write today.
- **Missing `<!-- desc: -->`**: indexer falls back to `[auto]` + first sentence. Functional, but the per-skill prompt row gets less specific.

## Authoring Checklist

1. Write frontmatter with `name`, a high-signal `description`, and a `allowed-tools` list (YAML list shape, not scalar). Use `[]` for prose-only skills.
2. Add the `<!-- desc: -->` first-body-line comment (agent-facing voice, dense).
3. Add concise execution steps in the SKILL body.
4. Include concrete paths/commands the agent should run.
5. Keep scope narrow; split broad domains into multiple skills.
6. Prefer deterministic instructions over generic advice.
7. Run `pytest tests/test_skill_conformance.py tests/test_skill_catalog.py` locally to catch frontmatter / allowed-tools drift before pushing.
8. After landing, run `mimir skills catalog` to refresh the catalog file.
