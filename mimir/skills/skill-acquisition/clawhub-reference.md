# ClawHub CLI Reference

ClawHub is the public skill registry for agent skills. Browse, search, install, publish, and manage skills.

Source: [github.com/openclaw/clawhub](https://github.com/openclaw/clawhub)
Website: [clawhub.ai](https://clawhub.ai)

## Install

No install needed — run via npx:

```bash
npx clawhub <command>
```

Or install globally: `npm i -g clawhub`

## Global Flags

| Flag | Description |
|------|-------------|
| `--workdir <dir>` | Working directory (default: cwd) |
| `--dir <dir>` | Install dir under workdir (default: `skills`) |
| `--site <url>` | Base URL for browser login (default: `https://clawhub.ai`) |
| `--registry <url>` | API base URL (default: discovered, else `https://clawhub.ai`) |
| `--no-input` | Disable interactive prompts |

Env equivalents: `CLAWHUB_SITE`, `CLAWHUB_REGISTRY`, `CLAWHUB_WORKDIR`

HTTP proxy supported: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`

## Commands

### Authentication

```bash
npx clawhub login                    # Browser-based GitHub OAuth
npx clawhub login --token clh_...   # Headless / CI
npx clawhub whoami                   # Verify stored token
```

Config location (macOS): `~/Library/Application Support/clawhub/config.json`
Override: `CLAWHUB_CONFIG_PATH`

### Discovery

```bash
# Vector search (natural language works)
npx clawhub search <query>

# Browse listings
npx clawhub explore                              # newest 25
npx clawhub explore --limit 50                   # more results
npx clawhub explore --sort newest                # default
npx clawhub explore --sort downloads             # most downloaded
npx clawhub explore --sort rating                # highest rated
npx clawhub explore --sort installs              # most installed
npx clawhub explore --sort installsAllTime       # all-time installs
npx clawhub explore --sort trending              # trending now
npx clawhub explore --json                       # machine-readable

# Output format: <slug>  v<version>  <age>  <summary>
```

### Inspection

```bash
npx clawhub inspect <slug>                       # metadata + description
npx clawhub inspect <slug> --version <version>   # specific version
npx clawhub inspect <slug> --tag <tag>           # tagged version (e.g. latest)
npx clawhub inspect <slug> --versions            # version history
npx clawhub inspect <slug> --versions --limit 50 # more versions
npx clawhub inspect <slug> --files               # list files in version
npx clawhub inspect <slug> --file SKILL.md       # raw file content (200KB limit)
npx clawhub inspect <slug> --json                # machine-readable
```

### Installation

```bash
# Install latest version
npx clawhub install <slug>

# Install to specific location
npx clawhub install <slug> --workdir ~/my-project --dir skills

# What it does:
#   1. Resolves latest version via /api/v1/skills/<slug>
#   2. Downloads zip via /api/v1/download
#   3. Extracts into <workdir>/<dir>/<slug>/
#   4. Writes lockfile: <workdir>/.clawhub/lock.json
#   5. Writes origin: <skill>/.clawhub/origin.json
```

### Management

```bash
npx clawhub list                     # Show installed skills (reads lock.json)
npx clawhub update <slug>            # Update specific skill
npx clawhub update --all             # Update all installed skills
npx clawhub update --force           # Overwrite local modifications
npx clawhub uninstall <slug>         # Remove (interactive confirmation)
npx clawhub uninstall <slug> --yes   # Remove (no confirmation)
```

Update behavior:
- Computes fingerprint from local files
- If fingerprint matches known version: updates silently
- If fingerprint differs (local edits): refuses by default, `--force` overwrites

### Social

```bash
npx clawhub star <slug>              # Star a skill
npx clawhub star <slug> --yes        # Skip confirmation
npx clawhub unstar <slug>            # Remove star
```

### Publishing

```bash
npx clawhub publish <path> \
  --slug my-skill \
  --name "My Skill" \
  --version 1.0.0 \
  --tags latest \
  --changelog "Initial release"

# Requirements:
#   - Must be logged in
#   - SKILL.md required with name + description in frontmatter
#   - Only text-based files (no binaries)
#   - Max bundle: 50MB
#   - Published under MIT-0 (free use, no attribution)
```

### Sync (Auto-Publish)

```bash
npx clawhub sync                     # Scan + publish interactively
npx clawhub sync --dry-run           # Preview only
npx clawhub sync --all               # Non-interactive
npx clawhub sync --root <dir>        # Extra scan roots
npx clawhub sync --bump minor        # Version bump (default: patch)
npx clawhub sync --changelog "text"  # Non-interactive changelog
npx clawhub sync --tags a,b,c        # Tags (default: latest)
npx clawhub sync --concurrency 4     # Parallel uploads
```

Auto-scans:
- Explicit `--root` directories
- Clawdbot workspace skills dirs (if configured)
- `~/.clawdbot/skills` (shared)

### Deletion / Moderation

```bash
npx clawhub delete <slug>            # Soft-delete (owner/mod/admin)
npx clawhub undelete <slug>          # Restore (owner/mod/admin)
npx clawhub hide <slug>              # Alias for delete
npx clawhub unhide <slug>            # Alias for undelete
```

### Ownership Transfer

```bash
npx clawhub transfer request <slug> <handle> [--message "..."]
npx clawhub transfer list [--outgoing]
npx clawhub transfer accept <slug>
npx clawhub transfer reject <slug>
npx clawhub transfer cancel <slug>
```

### Admin Commands

```bash
npx clawhub ban-user <handle> [--reason "..."] [--fuzzy]
npx clawhub set-role <handle> <role> [--fuzzy]
```

## API Endpoints

Key REST endpoints (base: `https://clawhub.ai`):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/search?q=...` | GET | Vector search |
| `/api/v1/skills?limit=N` | GET | List skills |
| `/api/v1/skills/<slug>` | GET | Skill metadata |
| `/api/v1/skills` | POST | Publish (multipart) |
| `/api/v1/download` | GET | Download skill zip |
| `/api/v1/stars/<slug>` | POST/DELETE | Star/unstar |
| `/api/v1/whoami` | GET | Verify auth |

## Telemetry

Minimal install telemetry during `npx clawhub sync` (for install counts).
Disable: `export CLAWHUB_DISABLE_TELEMETRY=1`
