---
name: pollers
description: Mechanics for building and managing pollers — subprocess scripts that check external services on a schedule and emit events when something has changed. Use when authoring a new poller (a `pollers.json` manifest plus a script in any language), debugging why a poller isn't firing, or extending an existing one. Pollers run on cron, emit JSONL events when there's something to report, and stay silent otherwise (silence-as-filter). The framework discovers `<home>/.claude/skills/<name>/pollers.json` files at startup and via `reload_pollers`; each emitted event becomes a fresh turn on a `poller:<name>` synthetic channel. Companion to the `world-scanning` skill, which catalogs *what's worth polling*. Distinct from `async-tasks` (one-shot wake-up via bash_async, not recurring) and from in-process scheduler callables (saga-consolidate, oauth-usage-poll — those mutate mimir-internal state and aren't subprocess-isolated).
allowed-tools:
  - Bash
  - Read
  - Write
  - reload_pollers
---

# Pollers — Event-Driven Monitoring

Pollers are lightweight scripts that check external services on a schedule and report back when something needs attention. They live inside skills and are discovered automatically by the scheduler.

## How It Works

1. A skill includes a `pollers.json` file alongside its `SKILL.md`
2. The scheduler discovers all `pollers.json` files at startup and when `reload_pollers` is called
3. On each cron tick, the scheduler runs the poller command as a subprocess
4. Each line of stdout is parsed as JSON and delivered to the agent as an event
5. If there's nothing to report, the poller outputs nothing — silence is the filter

## Creating a Poller

### 1. Write the poller script

The script runs with the skill directory as its **cwd**. It receives these environment variables automatically:

| Variable | Description |
|----------|-------------|
| `STATE_DIR` | Persistent cursor/state directory at `<home>/state/pollers/<poller_name>/` — created lazily on first run, on the home volume so cursor files survive container rebuilds even when the skill itself ships in the image. |
| `POLLER_NAME` | The poller's name from pollers.json (matches the `STATE_DIR` subpath). |

Plus any custom env vars from the `env` field, plus the agent's existing environment.

**Why STATE_DIR is separate from the skill dir**: skills are deployable artifacts (resettable via `seed_skills`, image-shippable, optionally reset on container rebuild). Cursor files are persistent runtime data — losing them means re-emitting the entire backlog of "events since cursor=0" on next run, which for a github-poller would be every PR comment in every watched repo. Mimir's filing rules separate these — skills under `.claude/skills/`, runtime state under `state/`.

**Command parsing (`pollers.json` `command` field)**: parsed by `/bin/sh -c` via `asyncio.create_subprocess_shell`. Shell features (env-var expansion `$FOO`, pipes, redirection) are available — and you're responsible for quoting args containing whitespace or shell metacharacters: `"python poller.py 'arg with spaces'"` not `"python poller.py arg with spaces"`.

**Output contract:**
- **stdout:** JSONL (one JSON object per line). Each line must have `prompt` (string); `poller` (string) is informational. Other keys flow into the resulting `AgentEvent.extra` so platform metadata (URLs, IDs, `source_platform`) carries through to your prompt rendering.
- **stderr:** Free-form diagnostic logging. Captured and emitted as a `poller_stderr` event in `events.jsonl` for observability — not forwarded as a turn-prompt, but greppable from `mimir introspection` / log scraping.
- **Exit 0:** Success. **Non-zero:** Error — any events emitted by this run are *dropped* (protects against half-failed runs sending partial event streams). The next cron tick retries.

Example poller script:

```python
#!/usr/bin/env python3
"""Check for new items since last poll."""
import json, os, sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", "."))
CURSOR_FILE = STATE_DIR / "cursor.json"

def load_cursor():
    if CURSOR_FILE.exists():
        return json.loads(CURSOR_FILE.read_text())
    return {}

def save_cursor(cursor):
    CURSOR_FILE.write_text(json.dumps(cursor, indent=2))

def main():
    cursor = load_cursor()
    # ... check your service, compare against cursor ...

    new_items = []  # your logic here

    for item in new_items:
        event = {
            "poller": os.environ.get("POLLER_NAME", "my-poller"),
            "prompt": f"New item: {item['title']}"
        }
        print(json.dumps(event))

    # Update cursor so next run skips these items
    save_cursor(cursor)

if __name__ == "__main__":
    main()
```

### 2. Create pollers.json in the skill directory

```json
{
  "pollers": [
    {
      "name": "my-service-check",
      "command": "python poller.py",
      "cron": "*/5 * * * *",
      "env": {
        "SERVICE_URL": "https://example.com/api"
      }
    }
  ]
}
```

**Top-level must be a dict** with a `pollers` key (not a bare array).

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier. Used in logs and event routing. |
| `command` | yes | Shell command, relative to the skill directory. |
| `cron` | yes | Cron expression (5-field, UTC). |
| `env` | no | Additional environment variables for the script. |
| `batch_size` | no | Coalesce up to N items per emitted AgentEvent (= per turn the agent sees). Default `1` (per-item-per-turn, matches open-strix). Use `>1` for bursty pollers (github-poller, RSS) so the agent sees one turn per cron tick instead of one per item. Items beyond `batch_size` overflow into additional batches with `batch_index` / `batch_count` set in `extra` so the agent can tell it's seeing part of a multi-batch fire. |

**On `batch_size`**: the poller script always emits per-item JSONL lines (clean contract). The framework collects all items, then emits `ceil(N/batch_size)` AgentEvents, each carrying a rendered prompt summarizing up to `batch_size` items + per-item metadata in `extra.items`. Single-item batches (default) render the prompt verbatim — no header. Multi-item batches render with a header (`<poller-name> reported N items` plus a `(batch X of Y)` suffix on multi-batch fires) and a numbered list of per-item prompts.

### 3. Register the pollers

After creating or updating `pollers.json`, call the `reload_pollers` tool. This re-scans `<home>/.claude/skills/**/pollers.json` and registers any new pollers with the scheduler. Removed pollers (skill uninstalled, manifest deleted) get dropped on the same call.

```
reload_pollers()
# → "reload_pollers ok: 2 poller(s) registered — github-activity, bluesky-mentions"
```

Pollers are also loaded automatically at startup, so a fresh container restart picks up any new skills without an explicit reload call.

## Ready-built poller skills

Mimir ships ready-built poller skills under ``mimir/optional-skills/`` — opt-in, NOT auto-installed (most installs don't need them). Each is a standalone skill directory with its own ``SKILL.md`` documenting the env vars and what it watches:

| Skill | Watches |
|---|---|
| `github-poller` | New issues, PRs, comments, PR reviews, and inline diff comments on configured GitHub repos |

To install one:

```
cp -r mimir/optional-skills/<name> <home>/.claude/skills/
# (set the skill's required env vars — see its SKILL.md)
reload_pollers
```

Removing a skill: delete the directory under `<home>/.claude/skills/` and call `reload_pollers` to drop the cron job.

## File Layout

```
skills/my-monitor/
├── SKILL.md
├── pollers.json        ← declares pollers
├── poller.py           ← the script
├── cursor.json         ← poller state (managed by script)
└── events.jsonl        ← optional local event log
```

## Design Patterns

See [design-patterns.md](design-patterns.md) for detailed guidance on:
- **State management** — cursor pattern, timestamp vs URI cursors, external service state, recovery on first run
- **Filtering** — selecting actionable notification types, avoiding shared `is_read` traps
- **Prompt quality** — including URIs/CIDs so the agent can act, not just observe
- **Error handling** — fail silently (exit non-zero), never emit on error
- **Anti-patterns** — common mistakes and how to avoid them

## Security & Privacy

See [security.md](security.md) for guidance on:
- **Trust tiers** — the follow-gate pattern for sorting trusted vs unknown sources
- **Operator in the loop** — keeping the human informed without locking everything down
- **Credential handling** — env vars, per-agent accounts, what not to log
- **Prompt injection** — honest reporting with context, not sanitization

## Key Constraints

- **60-second timeout.** If a poller doesn't finish in 60s, it's killed and the cycle is skipped. The framework reaps the subprocess on every exit path so long-lived mimir processes don't accumulate zombies.
- **Silence means nothing to report.** Only output lines when there's something actionable.
- **One JSON object per line.** Each line must parse independently.
- **`prompt` is the only required field.** Lines missing it are silently dropped (a poller can emit metadata-only diagnostic lines without firing turns). Other keys flow into the AgentEvent's `extra` for prompt rendering.
- **Pollers are dumb.** No LLM calls. Check a service, output what changed, exit. Keep them fast and pure.
- **State management is the poller's job.** Use `STATE_DIR` to store cursors, history, or any persistent state. The scheduler doesn't track state for you, but it does provide a persistent path under `<home>/state/pollers/<name>/` (see above).
- **16 KB prompt cap.** Each emitted event's `prompt` is capped at ~16 KB. Larger payloads get truncated with a marker — emit multiple events or stash to a file + send a path reference instead. Protects against chatty pollers blowing the prompt-build cache.
- **Back-pressure surfaces in events.jsonl.** When the dispatcher refuses an event (queue cap hit, channel saturated), it lands as a `poller_event_rejected` event; the run's `poller_complete` carries both `events_emitted` and `events_rejected` counts so a mismatch is grep-able.

## Debugging

If a poller isn't working:

1. **Check it was discovered:** `reload_pollers` reports the count and names
2. **Run it manually:** `cd skills/my-monitor && STATE_DIR=. POLLER_NAME=test python poller.py`
3. **Check stderr:** Poller stderr is logged as `poller_stderr` events
4. **Check exit code:** Non-zero exits are logged as `poller_nonzero_exit`
5. **Check JSON format:** Each stdout line must be valid JSON with at least a `prompt` key

## Available Tool

| Tool | Description |
|------|-------------|
| `reload_pollers` | Re-scan `<home>/.claude/skills/**/pollers.json` and register pollers. Call after installing or updating a skill. Also picks up skills with their `pollers.json` deleted (drops the corresponding cron jobs). |
