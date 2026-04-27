# Skillflag Reference

Skillflag is a CLI convention for bundling, listing, and installing agent skills directly from CLI tools — no registry upload required. Think "--help but for skills."

Source: [github.com/osolmaz/skillflag](https://github.com/osolmaz/skillflag)
Spec: [Skillflag Specification v0.1](https://github.com/osolmaz/skillflag/blob/main/docs/SKILLFLAG_SPEC.md)

## Core Concept

Any CLI tool can bundle skills and expose them via standardized flags:

```bash
<tool> --skill list              # What skills does this tool provide?
<tool> --skill show <id>         # Read a skill's SKILL.md
<tool> --skill export <id>       # Export as tar stream (for piping to installer)
```

The tool only knows how to LIST and EXPORT skills. Installation into specific agents is handled by a separate installer (`skill-install` / `npx skillflag install`).

## Discovery

```bash
# List all skills a tool bundles
<tool> --skill list
# Output: <id>\t<summary>  (one per line)

# JSON mode (includes integrity digest)
<tool> --skill list --json
# Output: { "skillflag_version": "0.1", "skills": [...] }

# Read a skill's documentation
<tool> --skill show <id>
# Output: contents of <id>/SKILL.md
```

JSON schema for `--skill list --json`:

```json
{
  "skillflag_version": "0.1",
  "skills": [
    {
      "id": "acpx",
      "summary": "Headless CLI client for ACP",
      "version": "1.0.0",
      "files": 3,
      "digest": "sha256:a1b2c3..."
    }
  ]
}
```

Fields: `id` (required), `summary` (optional), `version` (optional), `files` (optional), `digest` (required for integrity).

## Export

```bash
# Export skill as tar stream to stdout
<tool> --skill export <id>

# Inspect without installing
<tool> --skill export <id> | tar -tf -

# Extract manually
<tool> --skill export <id> | tar -x -C ~/skills/
```

Tar format:
- Single top-level directory `<id>/`
- Must contain `<id>/SKILL.md`
- No absolute paths, no path traversal
- Deterministic: stable order, normalized metadata (mtime=0, uid/gid=0)

## Installation via skill-install

```bash
# Install into a specific agent + scope
<tool> --skill export <id> | npx skillflag install --agent <agent> --scope <scope>

# Interactive wizard (picks agent + scope)
<tool> --skill export <id> | npx skillflag install

# Install from local directory
npx skillflag install ./skills/my-skill --agent claude --scope repo

# Custom destination (escape hatch for unlisted agents)
<tool> --skill export <id> | npx skillflag install --dest ~/agent-home/skills
```

### Agent Install Paths

| Agent | `--scope repo` | `--scope user` |
|-------|---------------|----------------|
| `claude` | `.claude/skills/<id>/` | `~/.claude/skills/<id>/` |
| `codex` | `.codex/skills/<id>/` | `~/.codex/skills/<id>/` |
| `vscode`/`copilot` | `.github/skills/<id>/` | unsupported |
| `amp` | `.agents/skills/<id>/` | `~/.config/agents/skills/<id>/` |
| `goose` | `.agents/skills/<id>/` | `~/.config/agents/skills/<id>/` |
| `portable` | `.agents/skills/<id>/` | `~/.config/agents/skills/<id>/` |
| `opencode` | `.opencode/skill/<id>/` | `~/.config/opencode/skill/<id>/` |
| `factory` | `.factory/skills/<id>/` | `~/.factory/skills/<id>/` |
| `cursor` | `.cursor/skills/<id>/` | unsupported (unconfirmed) |
| `pi` | `.pi/skills/<id>/` | `~/.pi/agent/skills/<id>/` |

### skill-install Flags

| Flag | Description |
|------|-------------|
| `--agent <name>` | Target agent (required unless `--dest`) |
| `--scope <scope>` | repo/user/cwd (required unless `--dest`) |
| `--dest <path>` | Override: install to `<path>/<id>/` |
| `--root <path>` | Override project root for `--scope repo` |
| `--mode copy\|link` | Copy files (default) or symlink |
| `--force` | Overwrite existing install |
| `--dry-run` | Preview without installing |
| `--json` | Machine-readable output |
| `--id <override>` | Override skill ID (folder name) |

### Conflict Handling

- If destination exists: **fail by default**
- `--force`: remove and replace
- No scripts are executed during installation (security requirement)

## Adding Skillflag to a CLI Tool

For tool authors who want to bundle skills:

### 1. Install the library

```bash
npm install skillflag
```

### 2. Create skill directory

```
skills/<skill-id>/
  SKILL.md         # Required: YAML frontmatter + markdown instructions
  other-file.md    # Optional: supporting docs
  scripts/         # Optional: executable scripts
```

### 3. Add to CLI entrypoint

```javascript
import { findSkillsRoot, maybeHandleSkillflag } from "skillflag";

await maybeHandleSkillflag(process.argv, {
  skillsRoot: findSkillsRoot(import.meta.url),
});
```

This intercepts `--skill list/show/export` and handles them automatically.

### 4. Verify

```bash
<your-tool> --skill list
<your-tool> --skill show <id>
<your-tool> --skill export <id> | tar -tf -
```

## Agent Skills Specification (agentskills.io)

All skills must conform to the [Agent Skills spec](https://agentskills.io/specification):

### SKILL.md Format

```yaml
---
name: my-skill            # Required. Lowercase, hyphens, 1-64 chars
description: What it does # Required. Max 1024 chars
license: MIT-0            # Optional
compatibility: Requires git, docker  # Optional. Max 500 chars
metadata:                 # Optional. Arbitrary key-value
  author: example-org
  version: "1.0"
allowed-tools: Bash(git:*) Read  # Optional. Experimental
---

# Skill instructions (markdown body)

Step-by-step instructions, examples, edge cases...
```

### Name Rules
- Lowercase letters, numbers, hyphens only
- 1-64 characters
- No consecutive hyphens (`--`)
- Must not start/end with hyphen
- Must match parent directory name

### Directory Structure

```
skill-name/
├── SKILL.md           # Required
├── scripts/           # Optional: executable code
├── references/        # Optional: additional docs
└── assets/            # Optional: templates, data files
```

### Progressive Disclosure Pattern
- **Metadata** (~100 tokens): `name` + `description` loaded at startup for ALL skills
- **Instructions** (<5000 tokens recommended): Full SKILL.md loaded on activation
- **Resources** (on demand): scripts/, references/, assets/ loaded when referenced

Keep SKILL.md under 500 lines. Move detailed references to separate files.

## Security

- Exporters must prevent path traversal and absolute paths
- Installers treat bundles as untrusted input
- `--dry-run` / `inspect` available for review before install
- Skills may include scripts — review before granting execution
- Integrity verification: installers should verify `sha256` digest from `--skill list --json`
