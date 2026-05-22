---
name: skill-acquisition
description: Discover, evaluate, install, and wrap external agent skills from ClawHub registry, skillflag-compliant CLI tools, and GitHub repos. Use when asked to find new capabilities, install a skill, browse what's available, or package a local skill for sharing.
subagent: true
allowed-tools:
  - Bash
  - Read
---

# Skill Acquisition

You help users discover and install agent skills from the ecosystem. There are three main sources:

1. **ClawHub** — Public skill registry (clawhub.ai) with vector search, versioning, and moderation
2. **Skillflag** — CLI tools that bundle their own skills via `--skill list/export`
3. **GitHub/Raw** — Skills as folders in git repos (SKILL.md + supporting files)

## When to Use This Skill

**USE when:**
- User asks to find/discover/search for skills or capabilities
- User wants to install a skill from ClawHub or a CLI tool
- User asks "is there a skill for X?"
- User wants to package/publish a local skill
- User asks about what skills are available

**DON'T USE when:**
- User wants to CREATE a new skill from scratch (that's skill-creator)
- User wants to modify an existing installed skill (just edit it)

## Prerequisites

Both tools run via `npx` — no global install needed, just npm:

```bash
# ClawHub CLI (primary discovery tool)
npx clawhub --help

# Skillflag installer (for CLI-bundled skills)
npx skillflag --help
```

## Discovery — Finding Skills

### 1. ClawHub Search (best for general discovery)

ClawHub uses vector search (OpenAI embeddings), so natural language queries work:

```bash
# Search by description
npx clawhub search "manage docker containers"
npx clawhub search "git workflow automation"

# Browse latest/trending
npx clawhub explore                          # newest 25
npx clawhub explore --sort trending          # trending now
npx clawhub explore --sort downloads         # most popular

# Machine-readable output
npx clawhub explore --json
npx clawhub search "kubernetes" --json
```

### 2. Skillflag Discovery (for CLI tools you already have)

Any skillflag-compliant CLI tool bundles its own skills:

```bash
# List skills a tool provides
<tool> --skill list

# See what a skill contains
<tool> --skill show <id>

# JSON metadata (includes digest for integrity)
<tool> --skill list --json
```

Known skillflag-compliant tools:
- `acpx` — ACP coding agent delegation
- Check any CLI you install: `<tool> --skill list` (won't break if unsupported)

### 3. GitHub Search (for skills not on ClawHub)

```bash
# Search GitHub for SKILL.md files
gh search code "filename:SKILL.md" --limit 20

# The openclaw/skills repo archives ALL ClawHub skills
# Browse: https://github.com/openclaw/skills/tree/main/skills
```

## Evaluation — Before Installing

Always evaluate before installing:

```bash
# Inspect without installing (ClawHub)
npx clawhub inspect <slug>                   # metadata + description
npx clawhub inspect <slug> --files           # list all files in the skill
npx clawhub inspect <slug> --file SKILL.md   # read the actual skill content

# Inspect a skillflag export
<tool> --skill show <id>                 # read SKILL.md content
<tool> --skill export <id> | tar -tf -   # list files without installing
```

**Evaluation checklist:**
1. Read the SKILL.md — does it do what you need?
2. Check required env vars / bins (`metadata.openclaw.requires`)
3. Check file count and what's included (scripts? templates?)
4. For ClawHub skills: check install count, version history, last update
5. For GitHub skills: check repo stars, recent activity, author reputation

## Installation — Getting Skills In Place

### From ClawHub

```bash
# Install to your skills directory
npx clawhub install <slug> --workdir "$(pwd)" --dir skills
```

### From Skillflag CLI Tools

```bash
# Into a custom directory (mimir agents)
<tool> --skill export <id> | npx skillflag install --dest ./skills
```

### From GitHub / Raw

```bash
# Clone just the skill directory
git clone --depth 1 <repo-url> /tmp/skill-source
cp -r /tmp/skill-source/skills/<name> ./skills/<name>
rm -rf /tmp/skill-source
```

### After Installing

Verify the skill appears in your prompt. Skills in `skills/` are automatically loaded.

## Wrapping — Adapting Skills for Your Agent

Raw skills from ClawHub/skillflag may need wrapping. The pattern:

### When to Wrap

- Skill assumes capabilities your agent doesn't have (e.g., coding ability)
- Skill needs context about WHEN to use it (delegation logic)
- Skill's SKILL.md is a CLI reference but you need behavioral guidance
- Multiple related skills should be combined into one coherent capability

### Wrapping Pattern

Create a wrapper skill that:
1. Has its own `SKILL.md` with behavioral instructions (when/how to use)
2. Includes the original skill as a reference doc (e.g., `<name>-reference.md`)
3. Adds any agent-specific context (delegation, reporting, error handling)

```
skills/my-capability/
  SKILL.md              <- your wrapper (behavioral instructions)
  <tool>-reference.md   <- original skill content (CLI/API reference)
```

Example: wrap `acpx` into a `coding` skill — SKILL.md teaches delegation behavior, `acpx-reference.md` has the full CLI reference.

## Publishing — Sharing Skills

```bash
# Login (GitHub OAuth)
npx clawhub login

# Publish a skill directory
npx clawhub publish ./skills/my-skill \
  --slug my-skill \
  --name "My Skill" \
  --version 1.0.0 \
  --tags latest \
  --changelog "Initial release"
```

**Requirements:**
- SKILL.md must have `name` and `description` in frontmatter
- Only text-based files (no binaries), max 50MB
- Published under MIT-0 (free use, no attribution required)

## Security Notes

- ClawHub has moderation and security analysis
- Skillflag exports are tar streams with path traversal protection
- Always `inspect` before installing from unknown sources
- Skills may include scripts — review them before granting execution

## References

- `/mimir/skills/skill-acquisition/clawhub-reference.md` — Full ClawHub CLI reference
- `/mimir/skills/skill-acquisition/skillflag-reference.md` — Skillflag specification and integration guide
- [ClawHub](https://clawhub.ai) — Browse skills in the browser
- [Agent Skills Spec](https://agentskills.io/specification) — The standard format
