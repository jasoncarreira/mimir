---
name: identity-lookup
description: Look up who an alias belongs to (people) or what a channel id is (channels) using state/identities.yaml. Use when the operator or a turn-context refers to a platform-prefixed id ("who is discord-100000000000000001?", "what channel is slack-C100?") or you need to resolve aliases the other direction (canonical → display name + aliases). Read-side only — the populator (mimir.identities_populator) maintains the registry.
success_criteria:
  # Lookup happens via Bash + YAML parsing of identities.yaml (the
  # portable form documented in the skill body), or the in-tree
  # IdentityResolver path. Both pin to identifiable command shapes.
  any_of:
    - tool_call:
        name: Bash
        args:
          command_glob: "*identities.yaml*"
    - tool_call:
        name: Bash
        args:
          command_glob: "*IdentityResolver*"
    - tool_call:
        name: Read
        args:
          file_path_glob: "*state/identities.yaml"
---

<!-- desc: Look up who an alias belongs to (people) or what a channel id is (channels) using state/identities.yaml. -->

# Identity Lookup

Read-side query skill against `state/identities.yaml`. The file carries
two parallel registries:

- **`people:`** — operator-managed canonical person ids and their
  platform aliases (`discord-<id>`, `slack-<id>`, `bsky:<handle>`,
  `email:<addr>`).
- **`channels:`** — operator-managed canonical channel ids with
  display names and `kind` (`public` | `dm` | `guild-meta` | other).

The daily populator (chainlink #44 / `mimir.identities_populator`)
auto-fills entries from connected bridges. This skill is for
*reading* — never edit `identities.yaml` from a turn; the file is
operator-managed and the populator handles updates.

## When to use

The operator or a recent-activity rendering surfaces a platform-prefixed
id and you need to know what / who it is:

- "Who is discord-100000000000000001?"
- "What channel is slack-C100 / discord-100000000000000002?"
- "What aliases does jason have?"
- "Is this a known person or just a raw id?"

Especially useful right after Phase C (chainlink #43) cross-channel
surfacing landed — recent-activity now spans all public channels, so
unfamiliar channel ids and author ids appear more often. This skill is
the canonical resolution path.

## How to use

The skill is a thin pattern over reading + parsing the YAML file. Use
Python (or `yq` if available) to query — `grep` works for quick checks
but YAML structure trips up greps for multi-line entries.

The bare-YAML examples below come first because they're portable
(work regardless of how mimir was installed); the typed
`IdentityResolver` path further down is the cleaner alternative when
you're already inside a Python process that has `mimir` importable.

### Resolve an alias to its canonical + record

```bash
python <<'PY'
import yaml, os
from pathlib import Path

home = Path(os.environ.get("MIMIR_HOME", os.environ["HOME"]))
doc = yaml.safe_load((home / "state" / "identities.yaml").read_text())
alias = "discord-100000000000000001"

for person in doc.get("people") or []:
    if alias in (person.get("aliases") or []) or person.get("canonical") == alias:
        print(f"canonical: {person['canonical']}")
        print(f"display:   {person.get('display_name', '(none)')}")
        print(f"aliases:   {', '.join(person.get('aliases') or [])}")
        if person.get("notes"):
            print(f"notes:     {person['notes']}")
        break
else:
    print(f"not found: {alias}")
PY
```

### Resolve a channel id

```bash
python <<'PY'
import yaml, os
from pathlib import Path

home = Path(os.environ.get("MIMIR_HOME", os.environ["HOME"]))
doc = yaml.safe_load((home / "state" / "identities.yaml").read_text())
channel_id = "slack-C100"

for ch in doc.get("channels") or []:
    if ch.get("canonical") == channel_id or channel_id in (ch.get("aliases") or []):
        print(f"canonical:    {ch['canonical']}")
        print(f"display:      {ch.get('display_name', '(none)')}")
        print(f"kind:         {ch.get('kind', '(unset)')}")
        if ch.get("notes"):
            print(f"notes:        {ch['notes']}")
        break
else:
    print(f"not found: {channel_id}")
PY
```

### List all people / channels

```bash
python <<'PY'
import yaml, os
from pathlib import Path

home = Path(os.environ.get("MIMIR_HOME", os.environ["HOME"]))
doc = yaml.safe_load((home / "state" / "identities.yaml").read_text())

print("=== people ===")
for p in doc.get("people") or []:
    name = p.get("display_name") or p["canonical"]
    aliases = ", ".join(p.get("aliases") or [])
    print(f"  {p['canonical']:24} {name:30} aliases: {aliases}")

print("=== channels ===")
for c in doc.get("channels") or []:
    name = c.get("display_name") or c["canonical"]
    kind = c.get("kind", "(unset)")
    print(f"  {c['canonical']:32} {name:30} kind: {kind}")
PY
```

### Programmatic resolution via IdentityResolver

The runtime already loads `identities.yaml` into
`mimir.identities.IdentityResolver`. From any Python process that
has `mimir` importable (the agent's own interpreter, or a debug
shell in the same environment):

```bash
python <<'PY'
import os
from pathlib import Path
from mimir.identities import IdentityResolver

home = Path(os.environ.get("MIMIR_HOME", os.environ["HOME"]))
r = IdentityResolver(home=home)
r.reload()

# People side
print(r.resolve("discord-100000000000000001"))           # → canonical (e.g. "alice")
print(r.display_name("discord-100000000000000001"))      # → "Alice Anderson"

# Channels side
print(r.resolve_channel("slack-C100"))                   # → canonical
print(r.channel_display_name("discord-100000000000000002"))
PY
```

`IdentityResolver` is also the right entry point for any future
in-process consumer — it normalizes the YAML into typed dataclasses
(`Identity`, `Channel`) and handles malformed-input warnings on read.

## On a cache miss

If the alias / canonical isn't in `identities.yaml`:

1. **Tell the operator the id isn't registered.** Don't pretend to know.
2. **Operator can add the entry manually** if they know the answer.
   The agent does NOT edit this file directly.
3. **If a daily populator is running** (chainlink #44 /
   `mimir.identities_populator`, opt-in via
   `MIMIR_IDENTITIES_POPULATE_CRON`) and the file's mtime is more
   than 24h old, suggest the operator re-run it — it scrapes
   connected Discord guilds + Slack workspaces and fills missing
   entries. Skip this step if the populator isn't wired up in the
   current deployment.

Live API fallback (Discord `fetch_member` / Slack `users.info`) is
*not* in the read path today. If a real recurring need surfaces,
chainlink it as a follow-up — the runtime would need to expose
bridge handles to the agent (a meaningful tool-surface expansion).

## What NOT to use this for

- **Don't write to `state/identities.yaml`** — operator-managed.
- **Don't use this for cross-channel author resolution at runtime** —
  that already happens automatically via `IdentityResolver` in the
  prompt-build path. This skill is for ad-hoc queries, not the hot
  path.
- **Don't use this skill for general identity discovery** ("find
  everyone who works on the engineering team") — there's no role /
  team metadata in the schema. Notes are free-form and not queryable
  beyond substring grep.

## See also

- `state/identities.yaml` — the registry itself, with header comments
  documenting the schema in full.
- `mimir/identities.py` — the loader (`IdentityResolver`) with
  read-side methods including `resolve_channel`,
  `channel_display_name`, `channel`, `all_channels`.
- `mimir/identities_populator.py` (chainlink #44) — the daily
  populator that fills entries from connected bridges.
- chainlink #40 — parent issue covering cross-channel content +
  identities/channels prompt-block expansion.
